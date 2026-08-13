"""Banana: spatial-memory benchmark target -- the "I am hungry" payoff."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="banana",
    label="🍌",
    title="Banana",
    collision="box",
    # 18cm long so it reads at 640x480 from a couple of metres away.
    size=(0.02, 0.09, 0.02),
    density=600,
    rgba=(0.95, 0.82, 0.15, 1.0),
    rest_z=0.02,
    reach=(0.30, 0.0),
)
