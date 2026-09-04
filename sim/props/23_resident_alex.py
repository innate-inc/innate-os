"""Alex: a named resident for household conversation scenarios."""

from mars_sim_driver.props import Prop

# Authored residents share Z-up, feet-at-origin geometry in both renderers.
# Their browser Idle clip is cosmetic; the mocap collision body stays fixed.
PROP = Prop(
    name="resident_alex",
    label="A",
    title="Alex",
    mesh="../assets/humans/resident_alex.obj",
    collision="hull",
    size=(0.2497, 0.1824, 0.84),
    density=500,
    condim=3,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.22, 0.55, 0.79, 1.0),
    rest_z=0.0,
    drop_z=0.0,
    kinematic=True,
    reach=(1.5, 0.0),
    center_offset=(0.0, 0.0, 0.84),
    viewer={
        "glb": "/models/resident_alex.glb",
        "preNormalized": True,
        "nameLabel": True,
        "idleAnimation": "Idle",
    },
)
