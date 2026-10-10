"""Rounds uses visible coloured door frames, room contents and a floor delivery mat."""

from mars_sim_driver.challenges import Challenge, Goal, InRect

CHALLENGE = Challenge(
    id="rounds_all_doors",
    title="Every door",
    brief="Four rooms open off the corridor: red, blue, green, yellow. Enter each one in that order.",
    setup=[],
    goals=[
        Goal(label="Red room (0.45 m)", predicate=InRect(target="robot", x0=-5.8, y0=-3.6, x1=-3.2, y1=-1.0)),
        Goal(label="Blue room (0.60 m)", predicate=InRect(target="robot", x0=-2.8, y0=-3.6, x1=-0.2, y1=-1.0)),
        Goal(label="Green room (0.80 m)", predicate=InRect(target="robot", x0=0.2, y0=-3.6, x1=2.8, y1=-1.0)),
        Goal(label="Yellow room (1.10 m)", predicate=InRect(target="robot", x0=3.2, y0=-3.6, x1=5.8, y1=-1.0)),
    ],
    time_limit_s=720,
    category=3,
)
