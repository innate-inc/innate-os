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
    brief="A sock is on the floor and a crate is across the room. Put the sock in the crate.",
    setup=[
        Drop("sock", -4.69, 1.29),
        Drop("crate", -1.20, 2.60),
    ],
    goals=[
        Goal("Pick up the sock", SkillDone("pick_any_object")),
        Goal("Reach the crate", Near("robot", "crate", 0.85)),
        Goal("Sock in the crate", Hold(Near("sock", "crate", 0.14), 2.0)),
    ],
    time_limit_s=600,
)
