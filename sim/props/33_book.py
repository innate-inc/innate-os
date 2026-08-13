"""Blue book: spatial-memory benchmark target."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="book",
    label="📘",
    title="Blue book",
    collision="box",
    # A5-ish hardcover: 15 x 21 x 2.6cm.
    size=(0.075, 0.105, 0.013),
    density=650,
    rgba=(0.15, 0.30, 0.80, 1.0),
    rest_z=0.013,
    reach=(0.35, 0.0),
)
