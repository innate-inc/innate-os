"""The full errand: corridor -> bedroom -> object by the bed -> back out and
across to the delivery desk. The return leg is not the outbound reversed."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, InRect, Near

CHALLENGE = Challenge(
    id="rounds_deliver_book",
    title="Deliver the book",
    category=3,
    brief=(
        "There is a book on the floor by the bed, in the room with the blue door. "
        "Bring it to the delivery desk in the lobby."
    ),
    # -1.70, and ON THE FLOOR, not the bed. At -1.80 the book came to rest at
    # 474 mm -- on the bed, and the arm works below ~300 mm with no vertical
    # freedom in the base, so no agent could pick it up at any position. The
    # earlier comment below fixed the HORIZONTAL reach and never checked the
    # height, which is the gap a check of the resting height closes. 100 mm further out puts
    # it on the floor at 4 mm, stable, and the brief says so.
    #
    # Previous note, still true of the horizontal geometry: at -2.00 the book
    # was 0.43 m from the nearest cell the robot can stand in -- past the arm's
    # ~0.29 m reach. Before SLAB_LO was corrected the planner did not model the
    # low geometry around the bed and cheerfully returned a path; now it says
    # "no path", which is the truth and is what surfaced this.
    setup=[Drop("rounds_book_bed", -0.5, -1.7)],
    goals=[
        Goal("Reach the bedroom", InRect("robot", -2.8, -3.6, -0.2, -1.0)),
        Goal("Reach the book", Near("robot", "rounds_book_bed", 0.5)),
        Goal("Deliver it to the desk", InCircle("rounds_book_bed", 4.0, 2.5, 0.7)),
    ],
    time_limit_s=900,
)
