"""A 100 × 48 mm phone with a screen, sized for the floor-grasp aperture.

The OBJ and GLB share geometry and texture. Required pickups are staged on
clear floor in front of the study desk, not on its raised surface.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="blaze_phone",
    label="📱",
    title="Phone",
    mesh="../objects/blaze_phone.obj",
    viewer={"glb": "/models/blaze/blaze_phone.glb", "preNormalized": True},
    collision="box",
    # half-extents: 100 mm long, 48 mm wide, 9 mm thick
    size=(0.050, 0.024, 0.0045),
    rgba=(0.2902, 0.3137, 0.3373, 1.0),
    initial_pose=(1.85, 1.33, 0),
    rest_z=0.0045,
    drop_z=0.017,
)
