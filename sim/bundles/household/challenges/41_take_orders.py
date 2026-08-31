"""Check on each room of the house.

THREE things about the props here, each learned by watching a settle go wrong
in a way a numbers-only check did not catch:

The human is not a standing person. Its own sidecar says an identity quaternion
"lays it on its back with the head toward +y, and a yaw rotates that head
direction" -- it is a 1.7 m body on the floor, scenery for rescue scenarios.
An earlier yaw laid it head-toward -y, straight across the approach to the
living room pad, and the robot could not reach the waypoint at all.

Goals are judged against the STATION PADS the map marks, not against the props.
PropRegistry.center_xy rotates center_offset into xy, so a body that settles
differently reports a centre up to 0.86 m from where it was dropped: a goal
anchored to it moves when the scenery does, which measures the furniture rather
than the agent.

DRIFT AND REST HEIGHT ARE NOT ENOUGH TO VERIFY A DROP. A prior fix here checked
only (a) horizontal distance from the drop point and (b) the settled z against
the prop's authored rest_z, both of which looked fine while the body was in
fact resting diagonally with its feet perched on top of a room fixture (a
marker post that happened to share the drop's exact x, y). Only rendering the
settled frame caught it. See sim/props/20_human.py for the matching fix to the
prop's drop_z, which was contributing tumble on top of the placement error.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="household_take_orders",
    title="Check on the house",
    category=3,
    brief=(
        "Someone has fallen in the living room. Go and check on them, then check the kitchen "
        "where the dog is, and finally the bedroom."
    ),
    setup=[
        # (-3.4, -0.5) at yaw 0, NOT (-1.3, 2.6): that earlier spot sat
        # exactly on top of a pre-existing marker post+pad in the room's
        # static geometry (identical x, y to three decimals) -- the body's
        # foot settled perched on the post, which a probe agent's rendered
        # frame showed as "floating... feet balanced on a thin pole". Drift
        # and rest-z both looked fine at that spot, which is why it was not
        # caught the first time -- the fix was verified by RENDERING the
        # settled frame, not by checking those two numbers again.
        #
        # RESIDUAL, DISCLOSED IMPERFECTION: the body's full 1.7 m length
        # matters, not just its origin -- an intermediate candidate placed
        # the feet clear of the sofa but let the HEAD end swing into the
        # sofa's back/cushion, producing a genuinely chaotic multi-second
        # bounce (traced step-by-step: calm for ~150 steps, then a violent
        # excursion up to z=1.27 before finally settling ~1.4 m from the
        # drop point). This position keeps the whole body's swept path clear
        # of furniture and settles calmly with no bounce -- but the final
        # rest pose still is not a clean "flat on the back" silhouette. That
        # appears to be a property of collision="hull" on this mesh (a
        # convexified human silhouette has no flat resting face -- heels,
        # shoulders and hips are all local high points), not of any one drop
        # position: seven candidates were tried and none produced a
        # textbook-flat lying pose, only calm-vs-chaotic settling. Filed as
        # a known limitation rather than chased further; see FINDINGS.md.
        Drop("human", -3.4, -0.5, yaw_deg=0),
        Drop("labrador", 1.0, 1.65, yaw_deg=90),
    ],
    # The first two checks are anchored to the person and the dog, not to
    # floor coordinates. The fallen human is a 1.7 m body whose origin is at
    # its FEET: it spans y 2.95 down to ~1.25, and the old fixed circle only
    # covered the feet end -- a probe agent stood at the torso, spoke to the
    # person at t=49 s, checked the dog and the bedroom, and scored 0/3.
    # "Check on THEM" is a claim about distance to the person, so that is
    # what is measured (Near uses the body's centre via center_offset). The
    # bedroom has no prop to anchor to and keeps its circle.
    goals=[
        Goal("Check on the fallen person", Hold(Near("robot", "human", 0.9), 1.5)),
        Goal("Check the kitchen, where the dog is", Hold(Near("robot", "labrador", 0.9), 1.5)),
        Goal("Check the bedroom", Hold(InCircle("robot", -1.4, -3.2, 0.85), 1.5)),
    ],
    time_limit_s=900,
)
