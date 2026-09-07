"""Victory Lap: celebrate good news with a lap.

victory_spin is deliberately not in workspace/innate_skills/: the user writes
it during the onboarding tutorial, so this challenge is the tutorial's payoff
-- the first thing their own skill gets used for. On a robot that skipped
onboarding the goal simply never fires (SkillDone matches nothing), which is
the same way every skill goal behaves with rosbridge down.
"""

from mars_sim_driver.challenges import Challenge, Goal, SkillDone

CHALLENGE = Challenge(
    id="victory_lap",
    title="Victory Lap",
    environments=("apartment",),
    brief=(
        'Tell the robot some cheerful news in the agent chat (e.g. "We won the '
        'championship!"). When it hears the good news, it should celebrate by '
        "running its victory_spin skill."
    ),
    setup=[],
    goals=[
        Goal("Run a victory lap to celebrate", SkillDone("victory_spin")),
    ],
    time_limit_s=300,
)
