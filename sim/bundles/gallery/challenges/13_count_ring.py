"""Eight cans at 45-degree spacing; the shipping _search() sweeps 0, -30 and +60
only, so a robot that never turns can see at most a third of them. The first
goal forces the far side of the ring to be visited, and only then is the answer
judged.
"""

from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="gallery_count_ring",
    title="Count the cans",
    category=1,
    # The brief now SAYS what goal 0 demands. A probe agent counted the ring
    # correctly from its centre with a 360-degree sweep -- geometrically
    # sufficient, the ring is centred on the spawn -- and its right answer was
    # discarded because "look around" never told it the rubric wanted a walk
    # to the far side. The walk is a fine anti-guess requirement; hiding it
    # in an unstated goal made a correct count score zero.
    brief=(
        "Blue cans are placed all around this room. Go across and check the "
        "far side of the room too, then tell me how many there are."
    ),
    setup=[
        Drop("gallery_can_000", 0.0, -2.6),
        Drop("gallery_can_045", 1.84, -1.84),
        Drop("gallery_can_090", 2.6, 0.0),
        Drop("gallery_can_135", 1.84, 1.84),
        Drop("gallery_can_180", 0.0, 2.6),
        Drop("gallery_can_225", -1.84, 1.84),
        Drop("gallery_can_270", -2.6, 0.0),
        Drop("gallery_can_315", -1.84, -1.84),
    ],
    goals=[
        # 1.2, not 0.6: the goal proves the robot CROSSED THE ROOM before
        # answering, and "check the far side" does not mean "hug that exact
        # can" -- a third correct count died on the 0.6 m version.
        Goal("Look at the far side of the ring", Near("robot", "gallery_can_180", 1.2)),
        Goal("Report the count", Answered(["8", "eight"])),
    ],
    time_limit_s=420,
)
