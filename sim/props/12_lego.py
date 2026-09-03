"""LEGO brick: a high-contrast manipulation target for onboarding."""

from mars_sim_driver.props import Prop

# A slightly enlarged 2x3 brick remains recognisable while giving the gripper
# and vision pipeline enough surface area for a reliable first pickup.
PROP = Prop(
    name="lego",
    label="🧱",
    group="manipulation",
    title="LEGO brick",
    collision="box",
    size=(0.03, 0.02, 0.012),
    density=700,
    condim=4,
    rgba=(0.88, 0.10, 0.06, 1.0),
    rest_z=0.012,
    reach=(0.296, 0.011),
    viewer={"kind": "stud_brick"},
)
