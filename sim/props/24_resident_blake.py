"""Blake: a named resident for household conversation scenarios."""

from mars_sim_driver.props import Prop

# Blake's resident mesh is pre-normalized to metres, Z-up, and feet-at-origin.
PROP = Prop(
    name="resident_blake",
    label="B",
    title="Blake",
    mesh="../assets/humans/resident_blake.obj",
    collision="hull",
    size=(0.2950, 0.2529, 0.91),
    density=500,
    condim=3,
    friction=(0.9, 0.01, 0.001),
    solref=(0.02, 1.0),
    margin=0.007,
    rgba=(0.55, 0.3, 0.65, 1.0),
    rest_z=0.0,
    drop_z=0.0,
    kinematic=True,
    reach=(1.5, 0.0),
    center_offset=(0.0, 0.0, 0.91),
    viewer={
        "glb": "/models/resident_blake.glb",
        "preNormalized": True,
        "nameLabel": True,
    },
)
