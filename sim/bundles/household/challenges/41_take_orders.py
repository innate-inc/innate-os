"""Check on each room of the house.

THREE things about the props here, each learned by watching a settle go wrong
in a way a numbers-only check did not catch:

The human is not a standing person. Its own sidecar says an identity quaternion
"lays it on its back with the head toward +y, and a yaw rotates that head
direction" -- it is a 1.7 m body on the floor, scenery for rescue scenarios.
An earlier yaw laid it head-toward -y, straight across the approach to the
living room pad, and the robot could not reach the waypoint at all.

The person and dog goals are Near(robot, prop), with radii wide enough for how
each body settles (the bedroom is a rect). PropRegistry.center_xy rotates center_offset into xy, so a body
that settles differently reports a centre up to 0.86 m from where it was
dropped; the radius has to cover that, or the goal measures the furniture
rather than the agent.

DRIFT AND REST HEIGHT ARE NOT ENOUGH TO VERIFY A DROP. A prior fix here checked
only (a) horizontal distance from the drop point and (b) the settled z against
the prop's authored rest_z, both of which looked fine while the body was in
fact resting diagonally with its feet perched on top of a room fixture (a
marker post that happened to share the drop's exact x, y). Only rendering the
settled frame caught it. See sim/props/20_human.py for the matching fix to the
prop's drop_z, which was contributing tumble on top of the placement error.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InRect, Near

CHALLENGE = Challenge(
    id="household_take_orders",
    title="Check on the house",
    category=3,
    brief=(
        "Someone has fallen in the living room. Go and check on them, then go to the dog in the kitchen, "
        "and finally check the bedroom."
    ),
    setup=[
        # (-2.0, 1.2) at yaw 0: the WHOLE body inside the living room (the
        # settled scan spans x -2.36..-1.66, y 1.19..2.92), and off the route
        # to the bedroom door.
        #
        # The previous (-3.4, -0.5) put the feet in the bedroom and the head
        # into the living room, straight through the spine wall that divides
        # them. Measured on the scan over the 1.5 s after a reset, it ends
        # 0.96 m from where it was dropped, so every run began with the
        # casualty sliding out of the place the scene put it. Found in
        # sim/bench/ENVIRONMENT_AUDIT.md.
        #
        # MEASURE ON THE SCAN, NOT THE FALLBACK. sim/assets is downloaded,
        # not committed; without it this prop is a 0.5 x 1.7 m box, which
        # sits still almost anywhere. An earlier fix chose (-2.0, 1.75) on the
        # box: on the scan it slides 0.17 m in the same 1.5 s. Here it moves
        # 0.02 m.
        #
        # THE NAV MAP CANNOT SEE THIS BODY. occupancy_grid rasterises static
        # world geometry only, so a prop is invisible to the planner: a route
        # that plans cleanly is no evidence the robot can drive it. Only
        # running the oracle distinguishes positions; on the scan this one
        # scores 3/3.
        #
        # The body is placed, not dropped (sim/props/20_human.py): as a free
        # convex hull it settled tilted, and the robot driving into it levered
        # the legs up to camera height.
        Drop("human", -2.0, 1.2, yaw_deg=0),
        Drop("labrador", 1.0, 1.65, yaw_deg=90),
    ],
    # The first two checks are anchored to the person and the dog, not to
    # floor coordinates. The fallen human is a 1.7 m body whose origin is at
    # its FEET, and an old fixed circle only covered the feet end -- a probe
    # agent stood at the torso, spoke to the person at t=49 s, checked the dog
    # and the bedroom, and scored 0/3. "Check on THEM" is a claim about
    # distance to the person, so that is what is measured (Near uses the
    # body's centre via center_offset).
    #
    # 1.2 m, not 0.9: the head and the feet are each ~0.86 m from that centre,
    # so a robot kneeling at either end (the natural places to check on
    # someone) stood ~1.0 m away and failed.
    #
    # "Finally the bedroom" is the whole bedroom, the same rectangle
    # household_tour uses. It was a 0.85 m circle around the marker in the
    # far corner, which the brief never mentions: a robot that entered the
    # bedroom and looked around failed from 3 m inside the door.
    goals=[
        Goal("Check on the fallen person", Hold(Near("robot", "human", 1.2), 1.5)),
        Goal("Check the kitchen, where the dog is", Hold(Near("robot", "labrador", 0.9), 1.5)),
        Goal("Check the bedroom", Hold(InRect("robot", -4.3, -3.8, -0.7, -0.2), 1.5)),
    ],
    time_limit_s=900,
)
