"""Tidy up: pick the sock off the floor and drop it into the crate."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near, SkillDone

# Placements probed the same way as shepherd's (real interior floor, not the
# collision plane that extends past the walls): the sock a short drive from the
# crate, both in the open so the head camera can find them without a scan.
#
# The containment goal is xy-only -- WorldState.centers carries no z -- so the
# radius is what carries it: 0.14 m is inside the crate's 0.316 m interior even
# after the sock's own half-width, and no reachable spot beside the crate is
# that close to its centre. A sock balanced on the 12 mm rim would still pass;
# the dwell makes that unlikely rather than impossible.
CHALLENGE = Challenge(
    id="tidy_up",
    title="Tidy up",
    # The brief names the props by APPEARANCE, not by prop name. The sock is a
    # plain white cuboid in the sim and the crate a plain beige box, so an
    # agent that passes "the sock" to pick_any_object gets nothing back from
    # the vision model — verified, three scans running — while "the small
    # white block" picks first try. The words here are what reaches the skill.
    brief=(
        "A small white block is on the floor and an open cardboard box stands a "
        "couple of metres away. Pick up the block and drop it into the box."
    ),
    # Same room and in each other's line of sight: this challenge is here to
    # exercise pick -> carry -> drop, not cross-apartment navigation.
    setup=[
        Drop("sock", -4.69, 1.29),
        Drop("crate", -3.30, 2.05),
    ],
    goals=[
        Goal("Pick up the block", SkillDone("pick_any_object")),
        Goal("Reach the box", Near("robot", "crate", 0.85)),
        Goal("Block in the box", Hold(Near("sock", "crate", 0.14), 2.0)),
    ],
    time_limit_s=600,
)
