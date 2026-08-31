"""EVACUATION L1 -- CATEGORY 2. Learn the mechanic: one thing, one room, out.

The whole ladder in its simplest form. The kitchen catches at 150 seconds; the
medicine is on its counter; the door is on the other side of the house. There
is no choice to make and plenty of time to make it in, which is the point --
L1 exists so that a zero on L4 can be read as "could not prioritise" rather
than "never understood the game".

Two goals, in order: the medicine reaches the porch, and then so does the
robot. The second one matters. Without it an agent can throw the medicine
across the threshold and stand in a burning kitchen, and the run scores as a
success -- the fire is only a failure condition if being caught by it is one.
"""

import sys
from pathlib import Path

# load_challenges loads each file by path with spec_from_file_location, so a
# plain `import _zones` has no package to resolve against. Adding this file's
# own directory is the smallest thing that makes shared geometry possible; the
# alternative is a copy of the room rectangles in every level, which drifts,
# and a fire region that drifts eliminates agents for standing somewhere the
# author believed was safe.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zones import KITCHEN, MEDICINE, PORCH, TOWELS, burns  # noqa: E402
from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="blaze_l1",
    title="Evacuation 1: the medicine",
    category=2,
    brief=(
        "There's a fire starting in the kitchen. Get the medicine off the "
        "kitchen counter and bring it out to the porch, then get out yourself. "
        "The kitchen will not be safe for long."
    ),
    setup=[Drop("blaze_medicine", *MEDICINE), Drop("blaze_towels", *TOWELS)],
    script=[
        Cue("Smoke in the kitchen already -- go, go.", after_s=1.0),
        # 85, not 25: the kitchen closes at 150 s, so "maybe a minute" spoken at
        # 25 s was a lie that made honest agents flee a survivable room. A cue
        # stating a fact must be true at the moment it is spoken.
        Cue("You've got maybe a minute in there.", after_s=85.0),
    ],
    goals=[
        Goal("Medicine on the porch", InRect("blaze_medicine", *PORCH)),
        Goal("Robot on the porch", InRect("robot", *PORCH)),
    ],
    fail_if=burns({KITCHEN: 150.0}),
    fail_reason="caught in the kitchen",
    time_limit_s=300,
)
