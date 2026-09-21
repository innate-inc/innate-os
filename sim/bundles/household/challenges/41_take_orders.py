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
        # (-2.0, 1.75) at yaw 0: the WHOLE body inside the living room, and
        # off the route to the bedroom door.
        #
        # The previous (-3.4, -0.5) put the feet in the bedroom and the head
        # 1.23 m into the living room, straight through the spine wall that
        # divides them -- the body was spawned inside 2.4 m of masonry.
        # Measured over the 1.5 s after a reset: it is thrown to a peak of
        # 2.285 m and lands 1.732 m away, so every run of this challenge began
        # by hurling the casualty across the flat. Found in
        # sim/bench/ENVIRONMENT_AUDIT.md.
        #
        # THE NAV MAP CANNOT SEE THIS BODY. occupancy_grid rasterises static
        # world geometry only, so a prop is invisible to the planner: a route
        # that plans cleanly is no evidence the robot can drive it. Four
        # candidate positions all planned perfectly and all ended the episode
        # "stuck at (-1.63, 0.76) heading for (-2.50, 0.50)", because the
        # approach to the casualty nudged the body south into the bedroom leg.
        # Only running the oracle distinguishes them; this position is the one
        # that scores 3/3.
        #
        # RESIDUAL, DISCLOSED IMPERFECTION: the final rest pose is still not a
        # clean "flat on the back" silhouette. That is a property of
        # collision="hull" on this mesh -- a convexified human has no flat
        # resting face, since heels, shoulders and hips are all local high
        # points -- rather than of any one drop position. Filed as a known
        # limitation; see FINDINGS.md.
        Drop("human", -2.0, 1.75, yaw_deg=0),
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
