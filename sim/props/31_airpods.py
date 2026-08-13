"""Airpods case: spatial-memory benchmark target -- the small find-my-object case."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="airpods",
    label="🎧",
    title="Airpods case",
    collision="box",
    # Case-sized (6 x 4.4 x 2.8cm): deliberately the HARD retrieval target,
    # only visible in frames captured within ~1.5m.
    size=(0.03, 0.022, 0.014),
    density=500,
    rgba=(0.93, 0.94, 0.96, 1.0),
    rest_z=0.014,
    reach=(0.30, 0.0),
)
