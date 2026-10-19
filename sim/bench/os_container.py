#!/usr/bin/env python3
"""Print the name of THIS checkout's innate-os container.

Upstream gives each checkout its own stack: the container is
innate-dev-<sha256(repo path)[:16]> (sim/launcher/config.py), and one machine
can run several. The helpers took the first `innate-dev*` docker listed, so
with two stacks up prime_brain.sh could reset and activate another checkout's
brain, say_brief.sh could brief its robot, and both reported success while the
benchmark's own robot heard nothing. Found by an adversarial review.

Order: this checkout's container if it is running; the legacy shared
`innate-dev` if that is; otherwise the single `innate-dev*` container when
there is exactly one, with a warning that it is not what the launcher would
have named ours; an error naming every candidate when there are several, or
none.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEGACY = "innate-dev"


def expected_name(repo: Path = REPO) -> str:
    """The launcher's OS_CONTAINER_NAME for a checkout, by the same rule
    (config.py: sha256 of the resolved repo path, first 16 hex digits).
    Restated rather than imported: config.py imports the launcher's dashboard
    and reads its config on import, and the shell helpers need only the name."""
    return "innate-dev-" + hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]


def running_containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=False)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def choose(expected: str, legacy: str, running: list[str]) -> tuple[str, str]:
    """(container, note). The container is "" when there is no single answer
    and the note says why; otherwise the note is empty, or a warning."""
    if expected in running:
        return expected, ""
    if legacy in running:
        return legacy, ""
    candidates = [name for name in running if name.startswith("innate-dev")]
    if len(candidates) == 1:
        return candidates[0], f"using {candidates[0]}, which is not this checkout's {expected}"
    if not candidates:
        return "", f"no innate-dev* container is running (this checkout's would be {expected})"
    return "", f"several innate-dev* containers are running and none is {expected}: {', '.join(candidates)}"


def main() -> int:
    name, note = choose(expected_name(), LEGACY, running_containers())
    if note:
        print(f"os_container: {note}", file=sys.stderr)
    if not name:
        return 1
    print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
