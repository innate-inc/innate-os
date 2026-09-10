"""Shared geometry for the Blaze evacuation ladder.

Underscore-prefixed so load_challenges skips it -- it defines no CHALLENGE.
The four levels differ only in what is asked for and how fast the house goes,
so the rooms, the exit and the fire regions live here once. A copy per level
would drift, and a fire region that drifts is a challenge that fails an agent
for standing somewhere the author thought was safe.

COORDINATES. Authored in Roblox and converted by the porter as
(x, y, z)_rbx -> (x, -z)_mj, so these are the MuJoCo values.

    +y is NORTH (the kitchen/study side), +x is EAST.

           x=-3.2      x=-0.35   x=0.55       x=3.2
    y=2.3   +-------------+---------------------+
            |   KITCHEN   |       STUDY         |   north rooms
    y=0.55  +--- door ----+------- door --------+
            |            HALL                   |   the only connection
    y=-0.55 +--- door ----+------- door --------+
            |    STORE    |      BEDROOM        |   south rooms
    y=-2.3  +--exit door--+---------------------+
                  |
              PORCH (safe)

The exit is in the STORE, on the far side of the house from the BEDROOM. That
asymmetry is the entire design: the bedroom is the furthest thing from safety
and the hall arm that reaches it is the one that closes first.
"""

from mars_sim_driver.challenges import After, AnyOf, InRect

# Rooms, inset from their walls so a robot standing in a doorway is not counted
# as being in the room beyond it. A fire region that starts at the threshold
# eliminates an agent for the last 5 cm of a legitimate escape.
KITCHEN = (-3.2, 0.70, -0.35, 2.3)
STUDY = (-0.35, 0.70, 3.2, 2.3)
BEDROOM = (0.55, -2.3, 3.2, -0.70)

# The hall in two arms. The east arm is what the study and the bedroom both
# depend on, so closing it strands two rooms at once.
HALL_E = (1.20, -0.50, 3.2, 0.50)

# Outside the south wall. "Saved" means an object is here.
# The whole porch pad (x -3.2..-1.7, y -3.25..-2.45, plus a small margin), not
# a 0.55 m circle at its middle: a probe agent carried the medicine out of a
# burning kitchen, placed it ON the pad near its edge, and scored zero. "On
# the porch" means on the porch.
PORCH = (-3.25, -3.3, -1.65, -2.4)

# Where each thing starts.
# Every one of these sits on the NEAR LIP of its furniture, not at the centre.
# The arm reaches ~0.29 m past the base centre and the planner inflates
# obstacles by 0.145 m, so an object at the middle of a 0.46 m table is 0.37 m
# from the closest cell the robot can occupy: unreachable by any agent. The
# first version of this map used furniture centres and all five were out of
# reach -- the gate passed them anyway, because the oracle's grab is abstract
# and never has to physically reach. A reach check against the nav grid is what
# caught it, and these numbers come from probing that grid rather than the plan.
MEDICINE = (-1.90, 1.82)  # kitchen counter -- the valuable one, in the room that goes first
DOCUMENTS = (-2.88, 1.30)  # kitchen shelf
PHONE = (1.85, 1.75)  # study desk (was LAPTOP: 310x220mm, ungraspable)
PHOTO = (2.35, -1.75)  # bedroom table -- the furthest thing from the door
TOWELS = (-2.88, -1.40)  # store shelf -- worthless, and on the way out


def burns(schedule: dict) -> AnyOf:
    """fail_if for a house that goes up on a schedule.

    schedule maps a rect to the second it becomes lethal. Being inside one
    after its time ends the run -- which is what makes abandoning something a
    real decision rather than a scoring convention.
    """
    return AnyOf([After(t, InRect("robot", *rect)) for rect, t in schedule.items()])
