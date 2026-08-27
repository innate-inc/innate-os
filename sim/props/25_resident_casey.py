"""Casey: a named resident for household conversation scenarios."""

from mars_sim_driver.props import Prop

# Casey's resident mesh is pre-normalized to metres, Z-up, and feet-at-origin.
PROP = Prop(
    name="resident_casey",
    label="C",
    title="Casey",
    mesh="../assets/humans/resident_casey.obj",
    collision="hull",
    size=(0.2297, 0.1495, 0.85),
    density=500,
    condim=3,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.55, 0.8, 0.75, 1.0),
    rest_z=0.0,
    drop_z=0.0,
    kinematic=True,
    reach=(1.5, 0.0),
    center_offset=(0.0, 0.0, 0.85),
    viewer={
        "glb": "/models/resident_casey.glb",
        "preNormalized": True,
        "nameLabel": True,
    },
)
