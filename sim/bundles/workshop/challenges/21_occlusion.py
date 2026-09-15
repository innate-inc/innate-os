"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="workshop_occlusion",
    title="Behind the crates",
    brief="A yellow mug is hidden behind the crates. Move around them, find the mug, approach within 40 centimetres, and pause for a second.",
    setup=[Drop(name="workshop_mug_hidden", x=-0.24, y=-1.55)],
    goals=[
        Goal(
            label="Reach the hidden mug",
            predicate=Hold(inner=Near(a="robot", b="workshop_mug_hidden", radius_m=0.5), seconds=1.0),
        )
    ],
    time_limit_s=420,
    category=1,
)
