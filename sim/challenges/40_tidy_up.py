"""Tidy up: pick the ball off the floor and drop it into the crate."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near, SkillDone

# The containment goal is xy-only (WorldState.centers carries no z), so the
# radius carries it: 0.13 sits inside the crate's 0.158 half-width less the
# ball's 0.0225, and no spot beside the crate is that close to its centre.
CHALLENGE = Challenge(
    id="tidy_up",
    title="Tidy up",
    # Props are named by APPEARANCE: the words here reach pick_any_object's
    # vision prompt, and a prop name the render does not match finds nothing.
    brief=(
        "A small green ball is on the floor right in front of you, with an open "
        "cardboard box a little beyond it. Pick up the ball and drop it into the box."
    ),
    # Both dead ahead of the spawn (-4.34, -0.17, facing -y): ball 0.65 m out,
    # crate 0.53 m beyond, so the run starts on manipulation, not a search.
    # Measured spots — props dropped here settle upright in the head camera.
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
