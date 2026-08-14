# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Tests for the root identity helper (scripts/identity/).

The BLE layer validates a blob and then forwards it unparsed, so these cover the half
that actually writes: what /etc/innate.env ends up holding, what never lands in it, and
the refusal that gates a second write.
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import SERVICE_KEY, VALID_ENV

REPO_ROOT = Path(__file__).resolve().parents[5]
IDENTITY_DIR = REPO_ROOT / "scripts" / "identity"


def _load(name: str):
    """Import a dash-named, extension-less script as a module."""
    path = str(IDENTITY_DIR / name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


applier = _load("innate-identity")
short_id_tool = _load("robot-short-id")


def _short_id_tool(*, serial=None, raw_serial=False):
    """Stand in for robot-short-id: a fixed serial, a fixed suffix."""
    return "1424523016164" if raw_serial else "a1b2"


@pytest.fixture
def robot(tmp_path):
    """An applier pointed at a throwaway rootfs, with root-only calls stubbed out."""
    repo = tmp_path / "innate-os"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "robot_info.json").write_text('{"robot_name": "MARS"}')
    system_env = tmp_path / "innate.env"

    with (
        patch.object(applier, "SYSTEM_ENV_PATH", system_env),
        patch.object(applier, "BACKUP_ENV_PATH", tmp_path / "innate.env.bak"),
        patch.object(applier, "REPO_ROOT", repo),
        patch.object(applier, "short_id_tool", side_effect=_short_id_tool),
        patch.object(applier, "set_password") as set_password,
        patch.object(applier.os, "chown"),
    ):
        yield type("Robot", (), {"repo": repo, "env_path": system_env, "set_password": set_password})


# ---------------------------------------------------------------------------
# robot-short-id
# ---------------------------------------------------------------------------


class TestShortId:
    def test_serial_derives_four_stable_hex_chars(self):
        first = short_id_tool.short_id("1424523016164")
        assert first == short_id_tool.short_id("1424523016164")
        assert len(first) == 4
        assert all(c in "0123456789abcdef" for c in first)

    def test_reads_the_device_tree_serial_stripping_the_nul(self, tmp_path):
        serial_file = tmp_path / "serial-number"
        serial_file.write_bytes(b"1424523016164\x00")

        with patch.object(short_id_tool, "SERIAL_PATH", serial_file):
            assert short_id_tool.module_serial() == "1424523016164"

    def test_missing_device_tree_reads_as_no_serial(self, tmp_path):
        with patch.object(short_id_tool, "SERIAL_PATH", tmp_path / "absent"):
            assert short_id_tool.module_serial() is None


# ---------------------------------------------------------------------------
# --write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_writes_identity_and_a_byte_identical_backup(self, robot, capsys):
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0

        written = robot.env_path.read_text()
        assert f"INNATE_SERVICE_KEY={SERVICE_KEY}" in written
        assert "MODULE_SERIAL=1424523016164" in written
        assert written == applier.BACKUP_ENV_PATH.read_text()
        assert robot.env_path.stat().st_mode & 0o777 == 0o640

        # The password rides its own field and is applied, never stored.
        assert "goodbot41" not in written
        robot.set_password.assert_called_once()
        assert robot.set_password.call_args[0][1] == "goodbot41"

        applied = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert applied == {"robot_id": "R7-41", "robot_name": "MARS the 41st"}

    def test_second_write_is_refused(self, robot):
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        before = robot.env_path.read_text()

        other = VALID_ENV.replace("R7-41", "R7-99")
        assert applier.run_write({"env": other, "password": "goodbot99"}) == applier.EXIT_ALREADY_PROVISIONED
        assert robot.env_path.read_text() == before

    def test_a_keyless_file_still_accepts_a_write(self, robot):
        """Deleting the key is how a robot is de-provisioned — the seeded defaults it
        leaves behind must not look provisioned."""
        robot.env_path.write_text("ROBOT_NAME=MARS-A1B2\nROBOT_ID=unprovisioned-a1b2\n")

        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        assert "R7-41" in robot.env_path.read_text()

    @pytest.mark.parametrize(
        "env",
        [
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nLD_PRELOAD=/tmp/evil.so\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nDYLD_INSERT_LIBRARIES=/tmp/evil\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nROBOT_ID=R7-41\x07\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nrobot id: R7-41\n",
            "ROBOT_ID=R7-41\n",
        ],
        ids=["ld_preload", "dyld", "control_char", "malformed_line", "no_service_key"],
    )
    def test_bad_blobs_write_nothing(self, robot, env):
        with pytest.raises(applier.PayloadError):
            applier.run_write({"env": env, "password": "goodbot41"})
        assert not robot.env_path.exists()

    def test_a_failed_write_leaves_the_robot_provisionable(self, robot):
        """Writing /etc/innate.env is the commit point, so a failure short of it must
        leave nothing that would make this helper refuse the retry."""
        real_write = applier.write_root_file

        def fail_on_system_env(path, text, group):
            if path == applier.SYSTEM_ENV_PATH:
                raise OSError("no space left on device")
            real_write(path, text, group)

        with patch.object(applier, "write_root_file", side_effect=fail_on_system_env):
            with pytest.raises(OSError):
                applier.run_write({"env": VALID_ENV, "password": "goodbot41"})

        # The .bak lands first as a canary — it is why a failure here is a surprise —
        # and the retry is what repairs the run, not a rollback.
        assert applier.BACKUP_ENV_PATH.exists()
        assert not applier.has_service_key(applier.read_system_env())
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0

    def test_oversized_env_is_refused(self, robot):
        env = VALID_ENV + "PADDING=" + "x" * applier.MAX_ENV_BYTES + "\n"

        with pytest.raises(applier.PayloadError):
            applier.run_write({"env": env, "password": "goodbot41"})

    def test_stale_repo_key_is_commented_out(self, robot):
        repo_env = robot.repo / ".env"
        repo_env.write_text("INNATE_SERVICE_KEY=innate-test-key-stale\nTELEMETRY_URL=https://logs.svc.innate.bot\n")

        applier.run_write({"env": VALID_ENV, "password": "goodbot41"})

        # The repo .env outranks /etc/innate.env, so a stale key would shadow the new one.
        text = repo_env.read_text()
        assert "\n# superseded" in "\n" + text
        assert "\nINNATE_SERVICE_KEY=innate-test-key-stale" not in "\n" + text
        assert "TELEMETRY_URL=https://logs.svc.innate.bot" in text


# ---------------------------------------------------------------------------
# --reset-info / --wipe-data
# ---------------------------------------------------------------------------


class TestHousekeeping:
    """Their own modes: both are useful on their own, and neither should be able to
    fail a provisioning run."""

    def test_reset_info_drops_the_derived_file(self, robot):
        assert applier.run_reset_info() == 0
        assert not (robot.repo / "data" / "robot_info.json").exists()

    def test_wipe_data_clears_the_bench_leftovers(self, robot):
        (robot.repo / "data" / "maps").mkdir()
        (robot.repo / "data" / "maps" / "bench.yaml").write_text("bench")
        (robot.repo / "data" / ".last_map").write_text("bench")
        (robot.repo / "workspace" / "custom_skills" / "scratch").mkdir(parents=True)
        (robot.repo / "workspace" / "innate_skills").mkdir(parents=True)
        (robot.repo / "workspace" / "innate_skills" / "pick.h5").write_text("shipped")

        assert applier.run_wipe_data() == 0

        assert list((robot.repo / "data" / "maps").iterdir()) == []
        assert not (robot.repo / "data" / ".last_map").exists()
        assert list((robot.repo / "workspace" / "custom_skills").iterdir()) == []
        # Shipped assets are not user data and nothing re-downloads them post-provision.
        assert (robot.repo / "workspace" / "innate_skills" / "pick.h5").exists()


class TestPassword:
    def test_plaintext_never_reaches_argv(self):
        with patch.object(applier.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="$6$hashed\n", stderr="")
            applier.set_password("jetson1", "goodbot41")

        openssl, chpasswd = mock_run.call_args_list
        assert "goodbot41" not in " ".join(openssl.args[0])
        assert openssl.kwargs["input"] == "goodbot41"
        assert "goodbot41" not in " ".join(chpasswd.args[0])
        assert chpasswd.kwargs["input"] == "jetson1:$6$hashed\n"


# ---------------------------------------------------------------------------
# --seed
# ---------------------------------------------------------------------------


class TestSeed:
    def test_seeds_serial_defaults_on_a_fresh_robot(self, robot):
        assert applier.run_seed() == 0

        env = applier.parse_env_text(robot.env_path.read_text())
        assert env["ROBOT_NAME"] == "MARS-A1B2"
        assert env["ROBOT_ID"] == "unprovisioned-a1b2"
        assert env["MODULE_SERIAL"] == "1424523016164"

    def test_leaves_a_provisioned_robot_alone(self, robot):
        """An R7.1 robot's key-only /etc/innate.env must survive the update."""
        robot.env_path.write_text(f"INNATE_SERVICE_KEY={SERVICE_KEY}\n")

        assert applier.run_seed() == 0
        assert robot.env_path.read_text() == f"INNATE_SERVICE_KEY={SERVICE_KEY}\n"

    def test_leaves_an_r7_0_robot_alone(self, robot):
        """Its key lives only in the repo .env, which outranks /etc/innate.env for
        readers — seeding unprovisioned defaults would make it lie about itself and
        reopen BLE provisioning on a robot that is already keyed."""
        (robot.repo / ".env").write_text(f"INNATE_SERVICE_KEY={SERVICE_KEY}\n")

        assert applier.run_seed() == 0
        assert not robot.env_path.exists()

    def test_never_triggers_on_the_robot_name(self, robot):
        """A user who renames their robot to exactly MARS must not be clobbered."""
        robot.env_path.write_text("ROBOT_NAME=MARS\nROBOT_ID=R7-41\n")

        assert applier.run_seed() == 0
        assert "R7-41" in robot.env_path.read_text()

    def test_refuses_to_run_without_a_module_serial(self, robot):
        """Keeps a dev machine from growing an /etc/innate.env."""
        with patch.object(applier, "short_id_tool", return_value=None):
            assert applier.run_seed() == 1
        assert not robot.env_path.exists()

    def test_reseeds_after_de_provisioning(self, robot):
        robot.env_path.write_text("ROBOT_NAME=MARS the 41st\nROBOT_ID=unprovisioned-a1b2\n")

        assert applier.run_seed() == 0

        assert applier.parse_env_text(robot.env_path.read_text())["ROBOT_NAME"] == "MARS-A1B2"
