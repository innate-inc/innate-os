"""Nowhere: the first run. A robot that can only wave, a white room, and a door out."""

from mars_sim_driver.challenges import Challenge, Goal

from .runtime import ACTS, ActDone, NowhereRuntime

RUNTIME = NowhereRuntime(ACTS)

CHALLENGE = Challenge(
    id="nowhere",
    title="Nowhere",
    brief="MARS just woke up with nothing. Talk to it, give it what it asks for, and find the way out.",
    prompt="Hello MARS. Let's find a way out together. Tell me what you need to get started.",
    environments=("void",),
    setup=[],
    goals=[Goal(act.label, ActDone(RUNTIME, index)) for index, act in enumerate(ACTS)],
    runtime=RUNTIME,
    agent_guidance=(
        "This is a scripted first run. The runtime field below describes the current act: what you can do, "
        "what you want next, and which skill the person may add next (runtime.wants). Skills you have "
        "not been added are not in your tools; you must ask the person to add them (the chat offers the "
        "button). The act's asking for a skill applies only while the skill is missing: if runtime.wants names a skill already "
        "in your tools because the person added it earlier, do not ask for it; say in one line, in your voice, that "
        "they already gave it to you and what it lets you do now, then use it for the act. An explicit request from the person takes priority over the current act: if it needs a skill "
        "that is not added, ask for that skill by name, even when runtime.wants names another. Resume the act "
        "after handling their request. Never pretend to have a skill or to have done something. The moment a new "
        "skill appears in your tools, use it. Keep replies to one or two sentences."
    ),
)
