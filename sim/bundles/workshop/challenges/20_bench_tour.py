"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="workshop_bench_tour",
    title="Bench tour",
    # 75 cm, not 60: each can stands 0.21 m behind its bench's front edge and
    # the robot's centre is 0.25 m behind its bumper, so 60 cm meant parking
    # within 14 cm of the bench -- inside Nav2's 0.3 m inflation. The cans
    # are 1.5 m apart, so the circles still do not overlap.
    brief="Five workbenches hold matching blue paint cans. Visit the benches from lowest to highest, stopping within 75 cm of each can.",
    setup=[
        Drop(name="workshop_target_06", x=-3.0, y=2.3),
        Drop(name="workshop_target_12", x=-1.5, y=2.3),
        Drop(name="workshop_target_18", x=0.0, y=2.3),
        Drop(name="workshop_target_24", x=1.5, y=2.3),
        Drop(name="workshop_target_30", x=3.0, y=2.3),
    ],
    goals=[
        Goal(label="Lowest bench", predicate=Near(a="robot", b="workshop_target_06", radius_m=0.75)),
        Goal(label="Second bench", predicate=Near(a="robot", b="workshop_target_12", radius_m=0.75)),
        Goal(label="Third bench", predicate=Near(a="robot", b="workshop_target_18", radius_m=0.75)),
        Goal(label="Fourth bench", predicate=Near(a="robot", b="workshop_target_24", radius_m=0.75)),
        Goal(label="Highest bench", predicate=Near(a="robot", b="workshop_target_30", radius_m=0.75)),
    ],
    time_limit_s=420,
    category=3,
)
