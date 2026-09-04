"""LEGO brick: a high-contrast manipulation target for onboarding."""

import math

from mars_sim_driver.props import Prop

# Twice a standard 2x3 brick in every dimension: still smaller than the first
# onboarding prototype, but large enough for MARS to see and grasp reliably.
PROP = Prop(
    name="lego",
    label="🧱",
    group="manipulation",
    title="LEGO brick",
    collision="box",
    size=(0.024, 0.016, 0.0096),
    density=700,
    condim=4,
    rgba=(0.88, 0.10, 0.06, 1.0),
    rest_z=0.0096,
    reach=(0.296, 0.011),
    placement_yaw=math.pi / 2,
    viewer={"kind": "stud_brick"},
)
