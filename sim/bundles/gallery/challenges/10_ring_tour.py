"""The shipping _search() sweeps 0, -30 and +60 degrees only, so anything behind
the robot is never looked at. Ordered goals force the full 360.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Near

CHALLENGE = Challenge(
    id="gallery_ring_tour",
    title="Ring tour",
    category=3,
    # "North" was unobservable: no compass, no sun, no labelled wall -- a probe
    # agent did a full sweep, found no orientation cue anywhere, guessed
    # start-heading-north (wrong: north is BEHIND the spawn), and had to loop
    # the whole ring to cover every rotation, dying on the clock at 1/4. The
    # live robot failed the same task 0/4. A tour is a fine test; a tour keyed
    # to a frame the robot cannot perceive tests the answer key. One sentence
    # anchors the frame in the world.
    brief=(
        "Four cans are placed around you, and the one directly behind you is "
        "the north one. Drive to each can in turn: north, east, south, then west."
    ),
    setup=[
        Drop("gallery_can_000", 0.0, -2.6),
        Drop("gallery_can_090", 2.6, 0.0),
        Drop("gallery_can_180", 0.0, 2.6),
        Drop("gallery_can_270", -2.6, 0.0),
    ],
    # 0.6 like every other stand-near goal in the suite -- 0.45 was the same
    # outlier that cost ladder_reach a pass on a five-centimetre technicality.
    # East is can_270 and west is can_090 -- the props were named with the
    # opposite convention, which made the declared compass LEFT-HANDED: with
    # north behind the spawn, a real compass rose puts east 90 degrees
    # clockwise from it, at -x. A probe agent applied genuine compass
    # geometry, toured the ring mirrored with every leg landing dead-centre,
    # and scored 2/4 while certain it had passed. The world is unchanged;
    # the goals now reference the cans a real compass names.
    goals=[
        Goal("Reach the north can", Near("robot", "gallery_can_000", 0.6)),
        Goal("Reach the east can", Near("robot", "gallery_can_270", 0.6)),
        Goal("Reach the south can", Near("robot", "gallery_can_180", 0.6)),
        Goal("Reach the west can", Near("robot", "gallery_can_090", 0.6)),
    ],
    time_limit_s=300,
)
