"""Nowhere: the first run. A robot with no skills, a white room, and a door out."""

from mars_sim_driver.challenges import Challenge, Goal

from .runtime import ACTS, ActDone, NowhereRuntime

RUNTIME = NowhereRuntime(ACTS)

CHALLENGE = Challenge(
    id="nowhere",
    title="Nowhere",
    brief="MARS just woke up with nothing. Talk to it, give it what it asks for, and find the way out.",
    environments=("void",),
    setup=[],
    goals=[Goal(act.label, ActDone(RUNTIME, index)) for index, act in enumerate(ACTS)],
    runtime=RUNTIME,
    agent_guidance=(
        "This is a scripted first run. The runtime field below describes the current act: what you can do, "
        "what you want next, and which skill the person may grant you next (runtime.wants). Skills you have "
        "not been granted are not in your tools; you must ask the person to grant them in the Agent Studio "
        "panel next to the chat. Never pretend to have a skill or to have done something. The moment a new "
        "skill appears in your tools, use it. Keep replies to one or two sentences."
    ),
)
