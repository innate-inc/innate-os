"""Red mug: spatial-memory benchmark target."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="mug",
    label="☕",
    title="Red mug",
    collision="cylinder",
    size=(0.04, 0.045),
    density=900,
    condim=4,
    friction=(1.0, 0.02, 0.005),
    rgba=(0.80, 0.15, 0.12, 1.0),
    rest_z=0.045,
    drop_z=0.8,  # released above coffee-table height so it can land ON furniture

    reach=(0.30, 0.0),
)
