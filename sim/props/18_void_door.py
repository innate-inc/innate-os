"""A doorway with no wall around it. The onboarding's way out of Nowhere."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="void_door",
    label="🚪",
    group="navigation",
    title="Door",
    collision="box",
    size=(0.06, 0.5, 1.05),
    rest_z=1.05,
    density=300,
    rgba=(0.05, 0.05, 0.06, 1.0),
    kinematic=True,
    viewer={"kind": "door_frame"},
)
