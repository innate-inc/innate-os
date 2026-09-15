"""The live helpers must fail when the thing they do fails.

Both scripts warn in their own headers that a silent no-op is the worst
outcome here: the brain stays inactive or never hears the brief, the agent sits
still, and the whole sweep scores zero for a reason nothing reports. They had
three ways to return success anyway -- an unchecked `docker cp`, a `docker exec`
whose status a pipe replaced, and an in-container Python that turned every
failed `ros2` call into a printed string.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent
SAY = BENCH / "say_brief.sh"
PRIME = BENCH / "prime_brain.sh"


def stub(d: Path, name: str, body: str) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run(script, bindir, stdin=""):
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )


@pytest.fixture
def bindir(tmp_path):
    return tmp_path


def test_no_container_fails(bindir):
    stub(bindir, "docker", "exit 0\n")  # `docker ps` prints nothing
    r = run(SAY, bindir, "brief")
    assert r.returncode != 0
    assert "no innate-dev" in r.stderr


@pytest.mark.parametrize("failing", ["cp", "exec"])
def test_a_failing_docker_step_fails_the_script(bindir, failing):
    stub(
        bindir,
        "docker",
        f"""case "$1" in
  ps) echo innate-dev-test; exit 0;;
  {failing}) exit 42;;
esac
exit 0
""",
    )
    assert run(SAY, bindir, "brief").returncode != 0, f"docker {failing} failed and say_brief returned 0"
    assert run(PRIME, bindir).returncode != 0, f"docker {failing} failed and prime_brain returned 0"


def test_the_brief_payload_fails_when_ros2_fails(bindir):
    """Docker succeeds, the Python runs, and every ros2 call fails. This is the
    case the outer status cannot see."""
    stub(bindir, "ros2", "exit 1\n")
    stub(
        bindir,
        "docker",
        """case "$1" in
  ps) echo innate-dev-test; exit 0;;
  cp) src=$2; dst=${3#*:}; cp "$src" "$dst"; exit 0;;
  exec) for a in "$@"; do case "$a" in /tmp/say_payload.*|/tmp/prime.*) f=$a;; esac; done
        [ -n "$f" ] || exit 0
        python3 "$f"; exit $?;;
esac
exit 0
""",
    )
    r = run(SAY, bindir, "the brief")
    assert r.returncode != 0, "every ros2 publish failed and say_brief returned 0"


def test_the_prime_payload_fails_when_ros2_fails(tmp_path):
    """Run the generated payload directly: the shell writes it, so this is the
    real thing rather than a transcription of it."""
    probe = tmp_path / "prime_probe.sh"
    text = PRIME.read_text()
    text = re.sub(r"^PRIME=.*$", f"PRIME={tmp_path / 'p.py'}", text, count=1, flags=re.M)
    text = re.sub(r"^trap 'rm -f \"\$PRIME\"' EXIT$", "", text, count=1, flags=re.M)
    probe.write_text(text)
    stub(tmp_path, "docker", "exit 0\n")
    run(probe, tmp_path)
    payload = tmp_path / "p.py"
    assert payload.exists(), "the script did not write its payload"

    stub(tmp_path, "ros2", "exit 1\n")
    r = subprocess.run(
        [sys.executable, str(payload)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert r.returncode != 0, "every ros2 call failed and the priming payload returned 0"
    assert "priming failed" in (r.stdout + r.stderr), "the failure was not named"


def test_the_brief_survives_quoting(bindir):
    """The brief is challenge text, not a shell literal."""
    stub(bindir, "ros2", "exit 0\n")
    stub(
        bindir,
        "docker",
        """case "$1" in
  ps) echo innate-dev-test; exit 0;;
  cp) cp "$2" "${3#*:}"; exit 0;;
  exec) for a in "$@"; do case "$a" in /tmp/say_payload.*) f=$a;; esac; done
        [ -n "$f" ] || exit 0
        python3 "$f"; exit $?;;
esac
exit 0
""",
    )
    nasty = 'Bring the "red" cup -- it' + chr(39) + "s $URGENT; rm -rf /; 100% now"
    assert run(SAY, bindir, nasty).returncode == 0


def test_the_container_is_this_checkouts() -> None:
    """With two stacks up, the helpers must not pick the other checkout's."""
    from os_container import choose

    assert choose("innate-dev-aaaa", "innate-dev", ["innate-dev-bbbb", "innate-dev-aaaa"]) == ("innate-dev-aaaa", "")
    assert choose("innate-dev-aaaa", "innate-dev", ["innate-dev", "innate-dev-bbbb"])[0] == "innate-dev"
    name, note = choose("innate-dev-aaaa", "innate-dev", ["innate-dev-bbbb", "postgres"])
    assert name == "innate-dev-bbbb" and "innate-dev-aaaa" in note
    name, note = choose("innate-dev-aaaa", "innate-dev", ["innate-dev-bbbb", "innate-dev-cccc"])
    assert name == "" and "several" in note
    name, note = choose("innate-dev-aaaa", "innate-dev", ["postgres"])
    assert name == "" and "no innate-dev" in note


def test_the_expected_name_is_the_launchers() -> None:
    """The naming rule is restated in os_container.py; pin it to the launcher's."""
    from os_container import REPO, expected_name

    launcher = subprocess.run(
        [sys.executable, "-c", "import config; print(config.OS_CONTAINER_NAME)"],
        cwd=REPO / "sim" / "launcher",
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip()
    assert expected_name() == launcher


def test_two_stacks_and_neither_ours_fails_loudly(bindir):
    stub(bindir, "docker", 'case "$1" in ps) echo innate-dev-one; echo innate-dev-two;; esac\nexit 0\n')
    r = run(SAY, bindir, "brief")
    assert r.returncode != 0
    assert "several innate-dev" in r.stderr
