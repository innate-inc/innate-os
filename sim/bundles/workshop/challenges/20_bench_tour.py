"""Identical cans on benches 0.06 -> 0.30 m tall: the only thing that changes
between goals is how high the target sits.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="workshop_bench_tour",
    title="Bench tour",
    category=3,
    brief="Five benches hold identical cans. Visit them from the lowest bench to the highest.",
    setup=[
        Drop("workshop_target_06", -3.0, 2.3),
        Drop("workshop_target_12", -1.5, 2.3),
        Drop("workshop_target_18", 0.0, 2.3),
        Drop("workshop_target_24", 1.5, 2.3),
        Drop("workshop_target_30", 3.0, 2.3),
    ],
    goals=[
        Goal("Lowest bench", Near("robot", "workshop_target_06", 0.6)),
        Goal("Second bench", Near("robot", "workshop_target_12", 0.6)),
        Goal("Third bench", Near("robot", "workshop_target_18", 0.6)),
        Goal("Fourth bench", Near("robot", "workshop_target_24", 0.6)),
        Goal("Highest bench", Near("robot", "workshop_target_30", 0.6)),
    ],
    time_limit_s=420,
)
