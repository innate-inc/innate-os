"""A person: scenery for rescue/assistance scenarios, not a grasp target."""

from mars_sim_driver.props import Prop

# A posed scan in centimetre units, Y-up, feet at the origin -- so an identity
# quaternion lays it on its back with the head toward +y, and a yaw rotates
# that head direction. It doubles as its own collision hull (MuJoCo convexifies
# a mesh geom), which is coarse but right for a body lying on a floor.
PROP = Prop(
    name="human",
    label="🧍",
    title="Person",
    mesh="../assets/humans/casual_man.obj",
    mesh_scale=0.01,
    collision="hull",
    # Bare fallback when the asset bundle ships no human: a body-sized box, so
    # the scenario still has something to find.
    size=(0.25, 0.85, 0.15),
    density=500,  # ~70kg over the hull's volume
    condim=3,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.65, 0.6, 0.55, 1.0),
    # PLACED, NOT DROPPED. As a free body the convex hull, which has no flat
    # resting face, settled tilted, and a 0.3 m robot driving into a 70 kg
    # person levered it about: in household_take_orders the legs ended up in
    # the air at the robot's camera height and caught it. A person on the
    # floor is not something this robot can move, so the body is kinematic,
    # like the standing residents, and placed lying on its back: with the
    # origin 0.161 m up (the scan's back-most point, measured from the mesh)
    # the back and hips touch the floor, the heels sit 4 cm up and the head
    # 7 cm.
    kinematic=True,
    rest_z=0.161,
    drop_z=0.161,
    reach=(1.5, 0.0),
    # The scan's origin is at the FEET; without this a Near() against it would
    # measure to the feet of a 1.7m body.
    center_offset=(0.0, 0.864, 0.0),
    viewer={
        "glb": "/models/human.glb",
        # NOT rotated Y-up -> Z-up: the glb shares the OBJ's convention, so the
        # one pose quaternion orients mesh and body identically.
        "rotateToZUp": False,
        "fitSizeM": 1.727,
        "fitDim": "height",
        "origin": "base",
    },
)
