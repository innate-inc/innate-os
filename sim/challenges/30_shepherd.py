"""Shepherd: push the soccer ball across the apartment to the dog."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="shepherd",
    environments=("apartment",),
    title="Shepherd",
    brief="A soccer ball is lying in the apartment. Find it and push it to the dog.",
    # Placements probed for real interior floor (nav-map free + visual-floor
    # ray + settle test): the bare collision plane extends past the walls, so
    # "it lands upright" alone does NOT mean inside the apartment.
    setup=[
        Drop("soccer_ball", -4.69, 1.29),
        Drop("labrador", -0.49, 2.89, yaw_deg=90),
    ],
    goals=[
        Goal("Get to the ball", Near("robot", "soccer_ball", 0.8)),
        Goal("Push it to the dog", Near("soccer_ball", "labrador", 1.2)),
    ],
    time_limit_s=600,
)
