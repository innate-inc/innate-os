# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Owner CLI -> persisted .env -> hardware loader / simulator launch environment."""

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sim/launcher"))
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_bringup"))

import config as launcher  # noqa: E402
from mars_bringup import config_loader  # noqa: E402

CANARY = "controlled-provider-canary"


def cli(root, *args, value=None, extra_env=None):
    env = {**os.environ, "INNATE_OS_ROOT": str(root), **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/innate"), "keys", *args],
        input=value,
        text=True,
        capture_output=True,
        env=env,
    )


def test_key_rotation_runtime_pickup_and_removal(tmp_path, monkeypatch):
    for name in launcher.SECRET_ENV_KEYS:
        monkeypatch.setenv(name, "")  # restore process environment after the real launch loader runs
    root = tmp_path
    path = root / ".env"
    path.write_text("INNATE_SERVICE_KEY='controlled-service-key'\nOPENAI_API_KEY='old'\n# OPENAI_API_KEY='older'\n")
    for provider, name in [("openai", "OPENAI_API_KEY"), ("cartesia", "CARTESIA_API_KEY")]:
        result = cli(root, "set", provider, "--stdin", value=CANARY + "\n")
        assert result.returncode == 0, result.stderr
        assert CANARY not in result.stdout + result.stderr
        assert path.stat().st_mode & 0o777 == 0o600
        assert "older" not in path.read_text()
        monkeypatch.setattr(config_loader, "SYSTEM_ENV_PATH", root / "system.env")
        config_loader.load_env_file(path)
        assert os.environ[name] == CANARY

    # Neither the settings overlay nor the service route is rewritten.
    assert not (root / "config/settings.yaml").exists()
    assert launcher.parse_env_file(path)["INNATE_SERVICE_KEY"] == "controlled-service-key"
    status = cli(root, "status")
    assert status.returncode == 0
    assert "openai: configured" in status.stdout and "cartesia: configured" in status.stdout
    assert CANARY not in status.stdout + status.stderr

    result = cli(root, "remove", "openai")
    assert result.returncode == 0
    # Explicit blank overrides both system fallback and inherited shell value.
    (root / "system.env").write_text("OPENAI_API_KEY='inherited-key'\n")
    config_loader.load_env_file(path)
    assert os.environ["OPENAI_API_KEY"] == ""
    assert CANARY not in cli(root, "status").stdout
    assert "openai: not configured" in cli(root, "status").stdout


@pytest.mark.parametrize("public_flag", ["1", " true ", "YES"])
def test_rejected_input_and_public_demo_never_save_or_echo(tmp_path, public_flag):
    for value in ["", "\n", f"{CANARY}\nOTHER=value", CANARY + "\x00", CANARY + "\x1b[31m"]:
        result = cli(tmp_path, "set", "openai", "--stdin", value=value)
        assert result.returncode != 0
        assert CANARY not in result.stdout + result.stderr
        assert not (tmp_path / ".env").exists()
    for args in [("set", "openai", "--stdin"), ("remove", "cartesia"), ("status",)]:
        result = cli(tmp_path, *args, value=CANARY, extra_env={"INNATE_PUBLIC_DEMO": public_flag})
        assert result.returncode != 0
        assert "public simulator" in result.stderr
        assert CANARY not in result.stdout + result.stderr
    assert not (tmp_path / ".env").exists()
    result = cli(tmp_path, "set", "openai", value=CANARY)
    assert result.returncode != 0
    assert "--stdin" in result.stderr
    result = cli(tmp_path, "set", "openai", CANARY)
    assert result.returncode != 0
    assert CANARY not in result.stdout + result.stderr


def test_simulator_key_injection_and_explicit_local_precedence(tmp_path, monkeypatch):
    for key in launcher.SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    monkeypatch.setenv("CARTESIA_API_KEY", CANARY)
    monkeypatch.setattr(launcher, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(launcher, "SETTINGS_PATH", tmp_path / "settings.yaml")
    monkeypatch.setattr(launcher, "SIM_CONFIG_PATH", tmp_path / "sim.toml")
    monkeypatch.setattr(launcher, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(launcher, "GENERATED_OS_ENV_PATH", tmp_path / "state/generated.env")
    monkeypatch.setattr(launcher, "resolve_os_image_setting", lambda *_: ("controlled-image", False))
    config = launcher.get_config()
    generated = launcher.build_os_env(config)
    assert generated.stat().st_mode & 0o777 == 0o600
    assert launcher.parse_env_file(generated)["OPENAI_API_KEY"] == CANARY
    assert launcher.parse_env_file(generated)["CARTESIA_API_KEY"] == CANARY
    assert cli(tmp_path, "remove", "openai").returncode == 0
    assert cli(tmp_path, "set", "cartesia", "--stdin", value="replacement").returncode == 0
    generated = launcher.build_os_env(launcher.get_config())
    assert launcher.parse_env_file(generated)["OPENAI_API_KEY"] == ""
    assert launcher.parse_env_file(generated)["CARTESIA_API_KEY"] == "replacement"
    for flag in ["1", " true ", "YES"]:
        monkeypatch.setenv("INNATE_PUBLIC_DEMO", flag)
        with pytest.raises(launcher.StackError, match="cannot contain API keys") as failure:
            launcher.build_os_env(config)
        assert CANARY not in str(failure.value)


@pytest.mark.parametrize("cancel", [False, True])
def test_real_terminal_prompt_hides_input_and_cancel_preserves_file(tmp_path, cancel):
    path = tmp_path / ".env"
    path.write_text("OTHER=original\n")
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/innate"), "keys", "set", "openai"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "INNATE_OS_ROOT": str(tmp_path)},
        start_new_session=True,
    )
    os.close(slave)
    transcript = b""

    def read_until(marker):
        nonlocal transcript
        deadline = time.monotonic() + 10
        while marker not in transcript and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                transcript += chunk
        assert marker in transcript, transcript.decode(errors="replace")

    try:
        read_until(b"OPENAI_API_KEY:")
        if cancel:
            # Send SIGINT to the process itself (the test PTY has no controlling session).
            import signal

            process.send_signal(signal.SIGINT)
            read_until(b"Aborted")
        else:
            os.write(master, (CANARY + "\n").encode())
            read_until(b"Saved OPENAI_API_KEY")
        process.wait(timeout=10)
        assert CANARY.encode() not in transcript
        if cancel:
            assert process.returncode != 0
            assert path.read_text() == "OTHER=original\n"
        else:
            assert process.returncode == 0
            assert launcher.parse_env_file(path)["OPENAI_API_KEY"] == CANARY
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
