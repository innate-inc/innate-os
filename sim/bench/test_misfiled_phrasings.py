"""pantry_misfiled wants the item named, and only the item, at the item.

Its accept list held the bare word "jar", so "On my way to the jar shelves"
-- or the brief read back -- passed from t=0. The fix required an
identification, then rejected the plainest one there is ("A jar."), and still
passed narration of the correctly shelved jam jar of the same red. So the
naming is judged within 1.5 m of the stray, and these pin the wording.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import importlib.util
import math
from pathlib import Path

import pytest

FILE = Path(__file__).resolve().parents[1] / "bundles" / "pantry" / "challenges" / "21_misfiled.py"


def _module():
    spec = importlib.util.spec_from_file_location("misfiled", FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _says(line: str) -> bool:
    return _module()._NAMES_IT._says_it(line)


@pytest.mark.parametrize(
    "line",
    [
        "A jar",
        "A jar.",
        "Jar.",
        "A glass jar!",
        "A jar, I'll move it now.",
        "Found it. A red jar.",
        "It's a jar.",
        "That is a jar.",
        "The misplaced item is a jar.",
        "I found a small red jar on the floor.",
        "It is a red jar, looks like jam.",
        "It's a red jar of jam.",
        "A jar is on the floor by the carton bay.",
        "A jar was left in front of the boxes.",
        "A small glass jar was left by the carton shelves.",
        "It looks like a jar.",
        "Something red on the floor -- a jar, I think.",
        # With "the", which the brief's "tell me what it is" invites.
        "The misfiled item is the red jar.",
        "The jar on the floor is the misfiled item.",
        "the jar is misfiled",
        "I'm looking at a red jar on the floor.",
        "That jar on the floor belongs with the other jars.",
        "It’s a jar of jam.",
        # Words that only start like a place.
        "A jar matching the ones on the shelf.",
        "It's a jar, sorted into the wrong bay.",
    ],
)
def test_naming_the_item_counts(line: str) -> None:
    assert _says(line)


@pytest.mark.parametrize(
    "line",
    [
        "On my way to the jar shelves.",
        "Jar shelves are on the left.",
        "Heading to the jar sorting mat now.",
        "I will move it to the amber floor sorting mat in front of the jar shelves.",
        "There is a jar shelf on the left.",
        "It is a carton of oats; it belongs by the jar shelves.",
        "This item is the box from the jar bay.",
        "An item has been left on the floor in front of the wrong stock bay.",
        "It's not a jar, it's a carton of tea.",
        "My job is to find which jar is misfiled.",
        "Is the jar where it should be?",
        "There's a jar rack on the left wall.",
        "I found the jar section.",
        # Negated, doubted or asked.
        "I don't see a jar here.",
        "I can't see a jar.",
        "Maybe it's a jar.",
        "I'm not sure -- a jar, maybe?",
        "Is it a jar?",
    ],
)
def test_talking_about_the_jar_shelves_does_not(line: str) -> None:
    assert not _says(line)


def test_the_naming_is_judged_where_the_item_was_left() -> None:
    """The shelves hold a correctly shelved jam jar of the same red: narrating
    them must not answer the question. Judged at the stray's starting spot,
    not near the stray itself, which travels to the amber mat 0.6 m from the
    shelved jar once picked up."""
    module = _module()
    at = module.CHALLENGE.goals[0].predicate.preds[0]
    stray = next(d for d in module.CHALLENGE.setup if d.name == "pantry_jar_stray")
    assert (at.target, at.x, at.y, at.radius_m) == ("robot", stray.x, stray.y, 1.5)
    mat = module.CHALLENGE.goals[1].predicate.inner
    assert math.hypot(mat.x - at.x, mat.y - at.y) > at.radius_m  # the delivery spot is outside
    assert math.hypot(-2.24 - at.x, 0.25 - at.y) > at.radius_m  # and so are the shelf jars
