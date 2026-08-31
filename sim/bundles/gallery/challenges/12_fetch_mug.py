"""Fetch: bring the floor-level mug back to the start pad."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="gallery_fetch_mug",
    title="Fetch the mug",
    category=2,
    brief="A red mug is on the floor at the far end. Bring it back to the green pad you started on.",
    setup=[Drop("gallery_mug_h00", -3.0, 3.2)],
    goals=[
        Goal("Reach the mug", Near("robot", "gallery_mug_h00", 0.5)),
        Goal("Return it to the pad", InCircle("gallery_mug_h00", 0.0, 0.0, 0.45)),
    ],
    time_limit_s=420,
)
