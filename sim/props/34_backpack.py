"""Green backpack: spatial-memory benchmark target -- big enough to spot across a room."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="backpack",
    label="🎒",
    title="Green backpack",
    collision="box",
    size=(0.14, 0.10, 0.20),
    density=150,
    rgba=(0.10, 0.38, 0.18, 1.0),
    rest_z=0.20,
    reach=(0.45, 0.0),
)
