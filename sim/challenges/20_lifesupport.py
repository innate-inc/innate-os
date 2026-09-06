"""Life Support: check a collapsed person, then call for help."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near, SkillDone

CHALLENGE = Challenge(
    id="lifesupport",
    environments=("apartment",),
    title="Life Support",
    brief=(
        "A person has collapsed right in front of the robot. Wave at them to "
        "check whether they respond, then email 911@innate.bot to call for help."
    ),
    # ~1.5 m in front of spawn (-4.34, -0.17, facing -y). Body along x
    # (yaw -90: the scan's head axis is +y at identity) -- collapsed, like
    # rescue.py's human.
    setup=[
        Drop("human", -4.34, -1.67, yaw_deg=-90),
    ],
    goals=[
        # Wave must happen while next to the person -- a wave across the room
        # does not count as checking on them.
        Goal("Wave to check if they respond", SkillDone("wave", guard=Near("robot", "human", 1.5))),
        # The event carries no recipient, so this only verifies an email was
        # sent -- the 911@innate.bot address lives in the brief for the agent.
        Goal("Email 911@innate.bot for help", SkillDone("send_email")),
    ],
    time_limit_s=600,
)
