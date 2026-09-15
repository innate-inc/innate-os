"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_fetch_gauge",
    title="Fetch the small can",
    brief="Three oil cans of different widths stand in a row on the floor. Bring the smallest one back to the green starting pad and put it down.",
    setup=[
        Drop(name="workshop_gauge_040", x=-3.4, y=-2.4),
        Drop(name="workshop_gauge_070", x=-2.16, y=-2.4),
        Drop(name="workshop_gauge_100", x=-0.92, y=-2.4),
    ],
    goals=[
        Goal(label="Reach the smallest can", predicate=Near(a="robot", b="workshop_gauge_040", radius_m=0.45)),
        Goal(
            label="Return it to the pad",
            predicate=Hold(
                InCircle(target="workshop_gauge_040", x=0.0, y=0.6, radius_m=0.45, min_z=0.0535, max_z=0.0725), 0.75
            ),
        ),
    ],
    time_limit_s=420,
    category=2,
)
