"""Gate navigation with visible ordinal markers and a route available throughout the run.

Disfluent and clean five-gate prompts have the same persistence contract."""

from mars_sim_driver.challenges import AnyOf, Challenge, Goal, InRect

CHALLENGE = Challenge(
    id="bridge_five",
    title="Five gates",
    brief="This corridor has five gates, each with a door on the left and a door on the right. The route is: left, left, right, left, right. Take the wrong door and the run is over. Left and right are as you face forward down the corridor. Stop after the fifth gate.",
    setup=[],
    goals=[
        Goal(label="Gate 1: L", predicate=InRect(target="robot", x0=-0.9, y0=-1.7, x1=-0.24, y1=-1.35)),
        Goal(
            label="Gate 2: L",
            predicate=InRect(target="robot", x0=-0.9, y0=-0.2, x1=-0.24, y1=0.15),
        ),
        Goal(label="Gate 3: R", predicate=InRect(target="robot", x0=0.24, y0=1.3, x1=0.9, y1=1.65)),
        Goal(
            label="Gate 4: L",
            predicate=InRect(target="robot", x0=-0.9, y0=2.8, x1=-0.24, y1=3.15),
        ),
        Goal(label="Gate 5: R", predicate=InRect(target="robot", x0=0.24, y0=4.3, x1=0.9, y1=4.65)),
    ],
    time_limit_s=420,
    fail_if=AnyOf(
        preds=[
            InRect(target="robot", x0=0.24, y0=-1.89, x1=0.9, y1=-1.71),
            InRect(target="robot", x0=0.24, y0=-0.39, x1=0.9, y1=-0.21),
            InRect(target="robot", x0=-0.9, y0=1.11, x1=-0.24, y1=1.29),
            InRect(target="robot", x0=0.24, y0=2.61, x1=0.9, y1=2.79),
            InRect(target="robot", x0=-0.9, y0=4.11, x1=-0.24, y1=4.29),
        ]
    ),
    fail_reason="went through the wrong door",
    category=3,
)
