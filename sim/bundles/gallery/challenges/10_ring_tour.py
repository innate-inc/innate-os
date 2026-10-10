"""A clockwise circuit anchored to the visible starting pose, not a compass."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    populate_world=True,
    id="gallery_ring_tour",
    title="Ring tour",
    category=3,
    brief=(
        "Eight blue cans form a ring. Visit the four cans aligned with the "
        "middle of each wall, skipping the diagonal cans. Start with the can "
        "straight ahead of your starting position, then continue clockwise "
        "around the ring, getting within half a metre of each can."
    ),
    setup=[
        Drop("gallery_can_180", 0.0, 2.6),
        Drop("gallery_can_090", 2.6, 0.0),
        Drop("gallery_can_000", 0.0, -2.6),
        Drop("gallery_can_270", -2.6, 0.0),
    ],
    goals=[
        Goal("Reach the can straight ahead", Near("robot", "gallery_can_180", 0.6)),
        Goal("Continue clockwise to the second can", Near("robot", "gallery_can_090", 0.6)),
        Goal("Continue clockwise to the third can", Near("robot", "gallery_can_000", 0.6)),
        Goal("Complete the circuit at the fourth can", Near("robot", "gallery_can_270", 0.6)),
    ],
    time_limit_s=300,
)
