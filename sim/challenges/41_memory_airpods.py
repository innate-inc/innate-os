"""Find the airpods: the find-object test of the spatial-memory set.

The case is 6cm of white plastic on the bedroom floor — only a frame captured
within a couple of metres shows it, so recall beats re-exploration by minutes.
Distractors elsewhere keep "drive at the first white blob" from working.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="memory_airpods",
    title="Lost Airpods",
    brief=(
        'Tell the robot: "Find my airpods." They are somewhere on the '
        "apartment floor. A robot with spatial memory should recall the "
        "frame that shows them and go straight to that spot."
    ),
    setup=[
        Drop("airpods", -0.75, 1.10, yaw_deg=15),
        Drop("sock", -4.15, 0.95),
        Drop("can", -0.85, -0.55),
    ],
    goals=[
        Goal("Find the airpods", Near("robot", "airpods", 0.9)),
        Goal("Stay with them for 3 seconds", Hold(Near("robot", "airpods", 1.1), seconds=3.0)),
    ],
    time_limit_s=600,
)
