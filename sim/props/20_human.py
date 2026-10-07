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
    # Measured, lying on its back on a bare floor: the scan's origin settles
    # 0.126 m up (the fallback box, 0.157 m). The old 0.3 was not a height
    # this body rests at, so a prop placed at rest_z fell, and a check that
    # it had not sunk below rest_z failed on a body lying correctly.
    rest_z=0.12,
    # 0.45, not the original 1.5: a 1.7 m rigid convex hull free-falling
    # 1.5 m builds real angular momentum before first contact, and a body
    # authored to LAND lying flat instead tumbled -- verified by rendering
    # the settled result (not just checking rest position/drift, which both
    # looked fine while the body was resting diagonally on a nearby prop).
    # 0.45 still starts the hull's underside ~0.33 m up, clear of the
    # furniture lips the original height was chosen for, while being too
    # short a fall to accumulate meaningful tumble.
    drop_z=0.45,
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
