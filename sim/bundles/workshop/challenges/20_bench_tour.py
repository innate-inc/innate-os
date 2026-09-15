"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="workshop_bench_tour",
    title="Bench tour",
    brief="Five workbenches hold matching blue paint cans. Visit the benches from lowest to highest, stopping within half a metre of each can.",
    setup=[
        Drop(name="workshop_target_06", x=-3.0, y=2.3),
        Drop(name="workshop_target_12", x=-1.5, y=2.3),
        Drop(name="workshop_target_18", x=0.0, y=2.3),
        Drop(name="workshop_target_24", x=1.5, y=2.3),
        Drop(name="workshop_target_30", x=3.0, y=2.3),
    ],
    goals=[
        Goal(label="Lowest bench", predicate=Near(a="robot", b="workshop_target_06", radius_m=0.6)),
        Goal(label="Second bench", predicate=Near(a="robot", b="workshop_target_12", radius_m=0.6)),
        Goal(label="Third bench", predicate=Near(a="robot", b="workshop_target_18", radius_m=0.6)),
        Goal(label="Fourth bench", predicate=Near(a="robot", b="workshop_target_24", radius_m=0.6)),
        Goal(label="Highest bench", predicate=Near(a="robot", b="workshop_target_30", radius_m=0.6)),
    ],
    time_limit_s=420,
    category=3,
)
