"""Tidy up: pick the ball off the floor and drop it into the crate."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near, SkillDone

# Placements probed the same way as shepherd's (real interior floor, not the
# collision plane that extends past the walls): the sock a short drive from the
# crate, both in the open so the head camera can find them without a scan.
#
# The containment goal is xy-only -- WorldState.centers carries no z -- so the
# radius is what carries it: the crate's interior half-width is 0.158 and the
# ball's radius 0.0225, so 0.13 is inside with room to spare, and no reachable
# spot beside the crate is that close to its centre. A ball perched on the
# 12 mm rim would still pass; the dwell makes that unlikely.
CHALLENGE = Challenge(
    id="tidy_up",
    title="Tidy up",
    # The brief names the props by APPEARANCE, not by prop name. The sock is a
    # plain white cuboid in the sim and the crate a plain beige box, so an
    # agent that passes "the sock" to pick_any_object gets nothing back from
    # the vision model — verified, three scans running — while "the small
    # white block" picks first try. The words here are what reaches the skill.
    brief=(
        "A small green ball is on the floor right in front of you, with an open "
        "cardboard box a little beyond it. Pick up the ball and drop it into the box."
    ),
    # Both dead ahead of the spawn pose (-4.34, -0.17, facing -y), so the run
    # starts on the manipulation rather than on a search: the block 0.65 m out
    # and the box 1.2 m beyond it, in that order. These two spots are measured,
    # not guessed — props dropped here settle upright at their rest heights
    # (block z=0.030, box z=0.070) with the robot's head camera on both.
    # The ball rather than the sock: its sidecar carries priority 4, so its own
    # friction (2.0, 0.4, 0.1) governs the grasp pair instead of the fingers'
    # (2.0, 0.15, 0.06) — four times the torsion, and torsion is what a carried
    # object works loose against. The block kept sliding out during the drive.
    setup=[
        Drop("ball", -4.34, -0.82),
        Drop("crate", -4.34, -1.35),
    ],
    goals=[
        Goal("Pick up the ball", SkillDone("pick_any_object")),
        Goal("Reach the box", Near("robot", "crate", 0.85)),
        Goal("Ball in the box", Hold(Near("ball", "crate", 0.13), 2.0)),
    ],
    time_limit_s=600,
)
