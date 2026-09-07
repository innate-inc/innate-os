"""A small green exit beacon at the end of the Backrooms corridor."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="exit_marker",
    label="↗",
    group="navigation",
    title="Green exit",
    collision="box",
    size=(0.04, 0.32, 0.45),
    rest_z=0.45,
    density=500,
    rgba=(0.08, 0.9, 0.28, 1.0),
)
