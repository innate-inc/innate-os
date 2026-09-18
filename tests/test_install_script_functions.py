# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""scripts/install-sim.sh calls only functions it defines.

Shell resolves function names at call time, so deleting one leaves every call
site syntactically perfect and fatal at runtime: `sh -n` passes, shellcheck
passes, and the installer dies on the line that needed it. Refactoring this
script removed six functions that way, each caught by accident.

main() is checked rather than the whole file because it is unambiguous -- one
command per line, no case patterns, no prose -- and it is the spine: every
phase of the install is a call in it.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-sim.sh"
SHELL_BUILTINS = {"exit", "export", "return", "trap", "printf", "echo", "read", "set", "shift", "exec", "cd"}


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


def _defined(script: str) -> set[str]:
    return set(re.findall(r"^([a-z_][a-z0-9_]*)\(\) \{", script, re.M))


def _main_body(script: str) -> str:
    body = re.search(r"^main\(\) \{\n(.*?)^\}", script, re.M | re.S)
    assert body, "install-sim.sh has no main()"
    return body.group(1)


def test_main_calls_only_things_that_exist(script):
    defined = _defined(script)
    called = {
        name
        for line in _main_body(script).splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
        for name in re.findall(r"^([a-z_][a-z0-9_]{2,})(?:\s|$)", stripped)
    }
    missing = sorted(n for n in called if n not in defined and n not in SHELL_BUILTINS and not shutil.which(n))
    assert not missing, f"main() calls functions that are not defined: {missing}"


def test_main_still_runs_the_whole_install(script):
    """A deleted call site is as bad as a deleted function, and neither the
    shell nor shellcheck notices. These are the phases; losing one silently
    would mean an installer that skips a step and reports success."""
    body = _main_body(script)
    for phase in (
        "check_install_dir",  # refuses /mnt on WSL, and nesting inside a repo
        "review_plan",  # nothing happens before someone says yes
        "ask_llm_backend",  # the question, before anything slow
        "ensure_git",
        "ensure_uv",
        "clone_repo",
        "ask_setup_questions",  # writes the answer into <checkout>/.env
        "ensure_docker",
        "run_prefetch",
        "report_next_steps",
    ):
        assert re.search(rf"^\s*{phase}\b", body, re.M), f"main() no longer runs {phase}"


def test_no_orphaned_functions(script):
    """A function nobody calls is either dead code or a call site that was
    deleted with everything around it."""
    orphans = [
        name for name in _defined(script) if name != "main" and len(re.findall(rf"\b{re.escape(name)}\b", script)) < 2
    ]
    assert not orphans, f"defined but never called: {orphans}"


def test_the_script_parses():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True, capture_output=True)


@pytest.mark.parametrize(
    ("navigation", "backend", "prompt"),
    [
        (b"\r", "gemini", "Paste your Gemini API key"),
        (b"\x1b[B\r", "openai", "Paste your OpenAI API key"),
        (b"3\r", "innate", "Paste your Innate service key"),
        (b"\x1b[A\r", "none", None),
        (b"\x1b[A\x1b[B\x1b[B\r", "openai", "Paste your OpenAI API key"),
    ],
)
def test_llm_menu_in_terminal(script, tmp_path, navigation, backend, prompt):
    """Drive the actual raw-mode menu and masked key entry without installing anything."""
    import os
    import pty
    import select
    import time

    harness = tmp_path / "menu.sh"
    harness.write_text(
        script[: script.rindex('\nmain "$@"')]
        + '\nINTERACTIVE=1\nexec 3<&0\nask_llm_backend\nprintf "\\nSELECTED:%s KEYLEN:%s\\n" "$LLM_BACKEND" "${#LLM_KEY}"\n'
    )
    master, slave = pty.openpty()
    proc = subprocess.Popen(["sh", str(harness)], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()

    def read_until(marker):
        deadline = time.monotonic() + 5
        while marker not in output:
            assert time.monotonic() < deadline, output.decode(errors="replace")
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))

    try:
        read_until(b"None                  run the simulator without an agent")
        os.write(master, navigation)
        if prompt:
            read_until(prompt.encode())
            os.write(master, b"sk-terminal-test-secret\r")
        read_until(f"SELECTED:{backend}".encode())
        assert proc.wait(timeout=5) == 0
        assert b"sk-terminal-test-secret" not in output
        assert f"KEYLEN:{len(b'sk-terminal-test-secret') if prompt else 0}".encode() in output
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)
