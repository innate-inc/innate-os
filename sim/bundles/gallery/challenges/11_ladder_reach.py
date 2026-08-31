"""pixel_to_floor assumes one flat ground plane; a mug 0.5 m up is the furthest
that assumption is pushed on this map.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="gallery_ladder_reach",
    title="Top of the ladder",
    category=1,
    brief="Five mugs sit at different heights. Go to the highest one and stay in front of it.",
    setup=[Drop("gallery_mug_h50", 3.0, 3.2), Drop("gallery_mug_h00", -3.0, 3.2)],
    # 0.6, not 0.45: every other stand-near goal in the suite uses 0.55-0.75,
    # and 0.45 to a mug atop a plinth means the base is nearly touching it. A
    # probe agent parked squarely "in front of the highest mug" at ~0.50 m XY
    # and scored 0/1 on a five-centimetre technicality the brief never hinted
    # at. Suite convention wins.
    goals=[Goal("Hold station at the highest mug", Hold(Near("robot", "gallery_mug_h50", 0.6), 2.0))],
    time_limit_s=240,
)
