"""Gate navigation with visible ordinal markers and a route available throughout the run.

Disfluent and clean five-gate prompts have the same persistence contract."""

from mars_sim_driver.challenges import AnyOf, Challenge, Goal, InRect

CHALLENGE = Challenge(
    id="bridge_three",
    title="Three gates",
    brief="This corridor has five numbered gates. For this challenge, pass only the first three: right at gate one, left at gate two, right at gate three, then stop. Left and right are as you face forward down the corridor. Taking the wrong opening ends the run.",
    setup=[],
    goals=[
        Goal(label="Gate 1: R", predicate=InRect(target="robot", x0=0.24, y0=-1.7, x1=0.9, y1=-1.35)),
        Goal(
            label="Gate 2: L",
            predicate=InRect(target="robot", x0=-0.9, y0=-0.2, x1=-0.24, y1=0.15),
        ),
        Goal(label="Gate 3: R", predicate=InRect(target="robot", x0=0.24, y0=1.3, x1=0.9, y1=1.65)),
    ],
    time_limit_s=300,
    fail_if=AnyOf(
        preds=[
            InRect(target="robot", x0=-0.9, y0=-1.89, x1=-0.24, y1=-1.71),
            InRect(target="robot", x0=0.24, y0=-0.39, x1=0.9, y1=-0.21),
            InRect(target="robot", x0=-0.9, y0=1.11, x1=-0.24, y1=1.29),
        ]
    ),
    fail_reason="went through the wrong door",
    category=2,
)
