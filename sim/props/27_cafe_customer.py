"""The customer at the counter: Alex's scan at the café's scale.

The counter world is built to the robot's size -- stools 0.21 m high, a 1.4 m
ceiling -- so a life-size resident would stand through the roof. Two counter
briefs are spoken by someone "at the middle seat", and an empty stool there
contradicted them; this is that person, in proportion to the room.
"""

from mars_sim_driver.props import Prop

SCALE = 0.29  # Alex is 1.68 m; 0.49 m beside a 0.21 m stool reads as an adult at a bar

PROP = Prop(
    name="cafe_customer",
    label="🙋",
    title="Customer",
    mesh="../assets/humans/resident_alex.obj",
    mesh_scale=SCALE,
    collision="hull",
    # Fallback when the asset bundle ships no scan: Alex's box, scaled.
    size=(0.2497 * SCALE, 0.1824 * SCALE, 0.84 * SCALE),
    density=500,
    condim=3,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.002,
    rgba=(0.75, 0.2, 0.2, 1.0),
    rest_z=0.0,
    drop_z=0.0,
    kinematic=True,
    reach=(0.5, 0.0),
    center_offset=(0.0, 0.0, 0.84 * SCALE),
    viewer={
        # The same Z-up, feet-at-origin glb as Alex, fitted to this height
        # instead of drawn at its own.
        "glb": "/models/resident_alex.glb",
        "rotateToZUp": False,
        "fitSizeM": round(1.68 * SCALE, 3),
        "fitDim": "height",
        "origin": "base",
    },
)
