# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the asset image's content hash and build context must agree on.

The hash names the tag the launcher PULLS; publish-sim-images.yml's `paths:`
filter decides whether that tag is ever BUILT; the dockerignore decides what
reaches the build. Three lists -- a drift between any two moves the tag
without moving the payload (or vice versa), and `up` 404s or ships stale
geometry. (No CI-vs-launcher parity test: the workflows import
config.compute_assets_image_inputs_hash, so there is no second copy.)
"""

import fnmatch
import importlib.util
import sys
from pathlib import Path

import pytest
from workflow_paths import covered, push_path_globs

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-sim-images.yml"

sys.path.insert(0, str(REPO_ROOT / "sim" / "launcher"))

DOCKERIGNORE = REPO_ROOT / "sim" / "Dockerfile.assets.dockerignore"


@pytest.mark.skipif(not DOCKERIGNORE.is_file(), reason="asset dockerignore not present")
def test_dockerignore_admits_exactly_the_work_subtrees():
    """EQUALITY with SIM_ASSET_UNITS, not coverage -- load-bearing: config.py is
    not itself a hashed input, so this test is what forces every unit-list
    change through the dockerignore, which IS hashed. A subset check would let
    a removal shrink /work while the content-addressed tag stayed put.
    """
    import config

    admitted = {
        line[len("!sim/assets/") :].split("/", 1)[0]
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.startswith("!sim/assets/")
    }
    expected = set(config.SIM_ASSET_UNITS)
    assert admitted == expected, (
        f"{DOCKERIGNORE.name} admits {sorted(admitted)} but config.SIM_ASSET_UNITS is "
        f"{sorted(expected)}.\n"
        f"  missing (seeded but invisible to docker): {sorted(expected - admitted) or 'none'}\n"
        f"  stale (admitted but no longer seeded):    {sorted(admitted - expected) or 'none'}"
    )


SEEDER = REPO_ROOT / "ci" / "seed_asset_context.py"


def _dockerignore_allows(relative_path: str, rules: list[str]) -> bool:
    """Does any `!` rule re-admit `relative_path` after the leading `**`?"""
    for rule in rules:
        pattern = rule[1:]
        if pattern.endswith("/**"):
            if relative_path.startswith(pattern[:-2]):
                return True
        elif fnmatch.fnmatch(relative_path, pattern):
            return True
    return False


@pytest.mark.skipif(not (SEEDER.is_file() and DOCKERIGNORE.is_file()), reason="asset tooling not present")
def test_dockerignore_admits_every_seeded_file():
    """A seeded file the allowlist omits is downloaded, then silently dropped
    before docker sees it -- every layer check still passes; the file is gone.
    """
    spec = importlib.util.spec_from_file_location("seed_asset_context", SEEDER)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    rules = [line for line in DOCKERIGNORE.read_text().splitlines() if line.startswith("!")]
    dropped = sorted(
        dest.relative_to(REPO_ROOT).as_posix()
        for dest in seed.RAW_FILES
        if not _dockerignore_allows(dest.relative_to(REPO_ROOT).as_posix(), rules)
    )
    assert not dropped, (
        f"{DOCKERIGNORE.name} does not admit these seeded files, so they are fetched and then "
        f"discarded before the build:\n  " + "\n  ".join(dropped)
    )


def test_household_resident_source_outputs_are_complete():
    spec = importlib.util.spec_from_file_location("seed_asset_context", SEEDER)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    relative = {path.relative_to(REPO_ROOT).as_posix() for path in seed.RAW_FILES}
    for resident in ("alex", "blake", "casey"):
        assert f"sim/assets/humans/resident_{resident}.obj" in relative
        assert f"sim/assets/humans/resident_{resident}_basecolor.png" in relative
        assert f"sim/viewer/public/models/resident_{resident}.glb" in relative


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="publish workflow not present")
def test_every_hashed_input_also_triggers_a_publish():
    """A hashed-but-unlisted file renames the image without building it: silent
    in CI, fatal on a user's machine asking GHCR for a tag nobody pushed.
    """
    import config

    globs = push_path_globs(WORKFLOW)
    hashed = [path.as_posix() for path in config.iter_assets_image_input_files(REPO_ROOT)]
    assert hashed, "no asset image inputs found -- the collector is broken"

    missing = sorted(path for path in hashed if not covered(path, globs))
    assert not missing, (
        "these files feed the asset image hash but no `paths:` entry in "
        "publish-sim-images.yml matches them, so editing one renames the image "
        "without publishing it:\n  " + "\n  ".join(missing)
    )


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="publish workflow not present")
def test_the_module_that_computes_the_hash_triggers_a_publish():
    """The test above covers the hash's INPUTS; this covers its DEFINITION.

    The input lists and the framing live in config.py, which is deliberately
    not an input of its own (that would retag on every launcher edit) -- so
    nothing above notices that editing it moves the tag. Derived from the
    module rather than spelled out, so moving the hash somewhere else moves
    this assertion with it.
    """
    import config

    owner = Path(config.__file__).resolve().relative_to(REPO_ROOT).as_posix()
    assert covered(owner, push_path_globs(WORKFLOW)), (
        f"{owner} defines every inputs-<hash> but no `paths:` entry in {WORKFLOW.name} matches "
        f"it, so editing an input list or the hash framing renames the image without publishing "
        f"it, and every `up` then 404s."
    )
