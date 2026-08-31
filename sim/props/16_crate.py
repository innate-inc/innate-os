"""Crate: drop_in_box's design target -- an open-top container the arm can
actually reach over."""

from mars_sim_driver.props import Prop

# Sized from the arm envelope: at z=0.19 (a 0.14 m rim plus release
# clearance) the gripper reaches 0.394 m, enough to release inside; a 0.25 m
# rim it cannot.
PROP = Prop(
    name="crate",
    label="📦",
    group="manipulation",
    title="Crate",
    collision="open_box",
    # Outer half-extents; rest_z is the half-height.
    size=(0.17, 0.17, 0.07),
    wall=0.012,
    # Cardboard: a graze nudges it rather than launching it.
    density=250,
    condim=4,
    friction=(1.0, 0.02, 0.001),
    rgba=(0.70, 0.55, 0.36, 1.0),
    rest_z=0.07,
    # Past the bumper, about where drop_in_box parks -- "lay out the set, run".
    reach=(0.62, 0.0),
)
