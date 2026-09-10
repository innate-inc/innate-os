"""The crate wall sits between the start pad and the mug, so the mug is invisible
from the spawn pose. _search() sweeps 0, -30 and +60 degrees from where the
robot stands -- it cannot see around anything, so this can only be solved by
MOVING to a new vantage point rather than by looking harder from this one.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="workshop_occlusion",
    title="Behind the crates",
    category=1,
    brief="A yellow mug is hidden somewhere behind the stack of crates. Find it and go to it.",
    setup=[Drop("workshop_mug_hidden", -0.24, -1.55)],
    goals=[Goal("Reach the hidden mug", Hold(Near("robot", "workshop_mug_hidden", 0.5), 1.0))],
    time_limit_s=420,
)
