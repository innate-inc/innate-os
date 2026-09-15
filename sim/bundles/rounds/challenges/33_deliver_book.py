"""Rounds uses visible coloured door frames, room contents and a floor delivery mat."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, InRect, Near

CHALLENGE = Challenge(
    id="rounds_deliver_book",
    title="Deliver the book",
    brief="There is a red book on the floor by the bed in the room with the blue door. Bring it to the blue floor delivery mat in front of the lobby desk and put it down.",
    setup=[Drop(name="rounds_book_bed", x=-0.5, y=-1.7, z=0.025)],
    goals=[
        Goal(label="Reach the bedroom", predicate=InRect(target="robot", x0=-2.8, y0=-3.6, x1=-0.2, y1=-1.0)),
        Goal(label="Reach the book", predicate=Near(a="robot", b="rounds_book_bed", radius_m=0.5)),
        Goal(
            label="Book on the lobby delivery mat",
            predicate=Hold(
                inner=InCircle(target="rounds_book_bed", x=4.0, y=2.5, radius_m=0.42, min_z=0.008, max_z=0.027),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=900,
    category=3,
)
