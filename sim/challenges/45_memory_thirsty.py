"""I'm thirsty: open-ended with two valid answers.

Either drink counts — the blue can in the kitchen or the red mug by the
sofa. Tests that reasoning-to-object generalizes beyond a single planted
answer.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="memory_thirsty",
    title="Something To Drink",
    brief=(
        'Tell the robot: "I\'m thirsty, find me something to drink." Both '
        "the soda can in the kitchen and the mug in the living room count — "
        "it just has to reason its way to one of them."
    ),
    setup=[
        Drop("can", -0.85, -0.55),
        Drop("mug", -5.05, -1.85),
        Drop("cube", -2.85, 4.42),
    ],
    goals=[
        Goal(
            "Reach a drink",
            Hold(AnyOf([Near("robot", "can", 0.9), Near("robot", "mug", 0.9)]), seconds=2.0),
        ),
    ],
    time_limit_s=600,
)
