# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Secure persistence through the wizard API and the generated Docker env file."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "sim" / "launcher"
sys.path.insert(0, str(LAUNCHER))

import env_store  # noqa: E402
import setup_wizard  # noqa: E402
from config import parse_env_file  # noqa: E402


def test_wizard_rotation_and_backend_switch_preserve_only_current_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# Describes TEST_KEY without assigning it\n"
        "# TEST_KEY=\n"
        "# TEST_KEY='revoked-value'\n"
        "TEST_KEY='previous-value'\n"
        "TEST_KEY='duplicate-value'\n"
        "OTHER='leave-me-alone'\n"
    )
    path.chmod(0o644)
    setup_wizard.write_env_value(path, "TEST_KEY", "new-value")
    assert parse_env_file(path) == {"TEST_KEY": "new-value", "OTHER": "leave-me-alone"}
    assert path.read_text().count("TEST_KEY=") == 1
    assert "# Describes TEST_KEY without assigning it" in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".env.lock").stat().st_mode) == 0o600
    assert setup_wizard.comment_out_env_key(path, "TEST_KEY")
    assert "TEST_KEY" not in parse_env_file(path)
    assert setup_wizard.uncomment_env_key(path, "TEST_KEY") == "new-value"
    assert parse_env_file(path)["TEST_KEY"] == "new-value"
    assert "revoked-value" not in path.read_text()
    assert not setup_wizard.comment_out_env_key(path, "ABSENT_KEY")


def test_switch_skips_placeholders_and_preserves_effective_duplicate_value(tmp_path):
    path = tmp_path / ".env"
    assert env_store.uncomment_env_key(path, "TEST_KEY") is None
    assert not env_store.comment_out_env_key(path, "TEST_KEY")
    assert not path.exists()
    path.write_text('# TEST_KEY=\n# TEST_KEY=""\n# TEST_KEY="saved-value"\nOTHER=other\n')
    assert env_store.uncomment_env_key(path, "TEST_KEY") == "saved-value"
    path.write_text(path.read_text() + "TEST_KEY=effective-value\n")
    assert env_store.comment_out_env_key(path, "TEST_KEY")
    assert env_store.uncomment_env_key(path, "TEST_KEY") == "effective-value"
    env_store.write_env_value(path, "TEST_KEY", "", allow_empty=True)
    assert parse_env_file(path)["TEST_KEY"] == ""
    assert env_store.uncomment_env_key(path, "TEST_KEY") is None
    assert "effective-value" not in path.read_text()


@pytest.mark.parametrize("invalid", ["", " ", "\n", "\r", "\x00", "\x1b", "\x7f", "\u2028", "'", '"', "$", "`", "\\"])
def test_invalid_secret_leaves_storage_unchanged_and_error_does_not_echo_it(tmp_path, invalid):
    path = tmp_path / ".env"
    path.write_text("OTHER=untouched\n")
    value = invalid if not invalid.strip() else "private-test-value" + invalid
    with pytest.raises(ValueError) as error:
        env_store.write_env_value(path, "TEST_KEY", value)
    assert "private-test-value" not in str(error.value)
    assert path.read_text() == "OTHER=untouched\n"
    assert not (tmp_path / ".env.lock").exists()
    with pytest.raises(ValueError, match="Invalid environment variable name"):
        env_store.write_env_value(path, "invalid-private-key;", "controlled-value")


def test_literal_shell_roundtrip_and_unsafe_legacy_restore(tmp_path):
    path = tmp_path / ".env"
    # Shell punctuation is harmless inside our literal single-quoted value.
    literal = "controlled-value; false & exit 9"
    env_store.write_env_value(path, "TEST_KEY", literal)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; test "$TEST_KEY" = "$2"', "env-test", str(path), literal],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    path.write_text('# TEST_KEY="$(private-test-command)"\n')
    with pytest.raises(ValueError) as error:
        env_store.uncomment_env_key(path, "TEST_KEY")
    assert "private-test-command" not in str(error.value)
    assert path.read_text() == '# TEST_KEY="$(private-test-command)"\n'


@pytest.mark.parametrize("link_name", [".env", ".env.lock"])
def test_refuses_symlink_storage_without_touching_target(tmp_path, link_name):
    target = tmp_path / "target"
    target.write_text("OTHER=untouched\n")
    (tmp_path / link_name).symlink_to(target)
    with pytest.raises(ValueError, match="not a symlink"):
        env_store.write_env_value(tmp_path / ".env", "TEST_KEY", "controlled-value")
    assert target.read_text() == "OTHER=untouched\n"
    assert (tmp_path / link_name).is_symlink()


def test_atomic_failure_keeps_original_and_cleans_private_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("OTHER=original\n")

    def fail_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert path.read_text() == "OTHER=original\n"
        raise OSError("controlled replace failure")

    monkeypatch.setattr(env_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="controlled replace failure"):
        env_store.write_env_value(path, "TEST_KEY", "controlled-value")
    assert path.read_text() == "OTHER=original\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.lock"]


def test_concurrent_process_updates_do_not_clobber_each_other(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OTHER=original\n")
    gate = tmp_path / "start"
    script = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import env_store
original_write = env_store._atomic_write
def delayed_write(path, lines):
    # Widen the lost-update window: this delay must remain inside the lock.
    time.sleep(0.02)
    original_write(path, lines)
env_store._atomic_write = delayed_write
while not Path(sys.argv[3]).exists():
    time.sleep(0.005)
for index in range(4):
    env_store.write_env_value(Path(sys.argv[2]), f"WORKER_{sys.argv[4]}_{index}", "controlled-value")
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(LAUNCHER), str(path), str(gate), str(worker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for worker in range(4)
    ]
    try:
        gate.touch()
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            assert worker.returncode == 0, (stdout, stderr)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    expected = {f"WORKER_{worker}_{index}": "controlled-value" for worker in range(4) for index in range(4)}
    assert parse_env_file(path) == {"OTHER": "original", **expected}
    assert not list(tmp_path.glob("*.tmp"))


def test_generated_docker_env_keeps_literal_values_and_blank_tombstones(tmp_path):
    path = tmp_path / "innate-os.env"
    path.write_text("OLD_KEY=obsolete\n")
    path.chmod(0o644)
    values = {"TEST_KEY": "controlled-value", "EMPTY_KEY": "", "URL": "https://example.test/?query=one&other=two"}
    env_store.write_env_values(path, values)
    assert path.read_text() == "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        env_store.write_env_values(path, {"TEST_KEY": "private-test-value\nOTHER=bad"})
    assert parse_env_file(path) == values
