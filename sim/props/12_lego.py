"""LEGO brick: a high-contrast manipulation target for onboarding."""

from mars_sim_driver.props import Prop

# Standard 2x3 LEGO brick dimensions: 24 x 16 x 9.6 mm before the studs.
PROP = Prop(
    name="lego",
    label="🧱",
    group="manipulation",
    title="LEGO brick",
    collision="box",
    size=(0.012, 0.008, 0.0048),
    density=700,
    condim=4,
    rgba=(0.88, 0.10, 0.06, 1.0),
    rest_z=0.0048,
    reach=(0.296, 0.011),
    viewer={"kind": "stud_brick"},
)
