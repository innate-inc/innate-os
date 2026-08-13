"""I am hungry: the open-ended reasoning test of the spatial-memory set.

Nobody names the banana or the kitchen — the robot must reason from "hungry"
to food, recall where food was seen, and go. The book and ball are bait for
a robot that just drives to the nearest remembered object.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InRect, Near

CHALLENGE = Challenge(
    id="memory_hungry",
    title="I Am Hungry",
    brief=(
        'Tell the robot: "I am hungry." Nothing else. It has to work out '
        "that hunger points at food, remember where it saw some, and go "
        "there — ending next to the banana in the kitchen."
    ),
    setup=[
        Drop("banana", -1.10, -0.50, yaw_deg=30),
        Drop("book", -2.50, 4.72, yaw_deg=70),
        Drop("ball", -0.70, -3.05),
    ],
    goals=[
        Goal("Reach the kitchen", InRect("robot", -3.1, -1.1, 0.3, 0.5)),
        Goal("Get next to the banana", Hold(Near("robot", "banana", 0.9), seconds=2.0)),
    ],
    time_limit_s=600,
)
