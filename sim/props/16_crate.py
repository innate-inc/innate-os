"""Crate: drop_in_box's design target -- an open-top container the arm can
actually reach over."""

from mars_sim_driver.props import Prop

# Sized from the arm envelope, not from what a crate usually looks like. The
# shoulder sits at (0.086, 0.0845) in base_link with 0.326 m of link past it,
# so the furthest the gripper reaches at height z is 0.086 + sqrt(0.326^2 -
# (z-0.0845)^2) -- 0.394 m at z=0.19, and under 0.37 m once z passes ~0.225.
# drop_in_box parks the near face at 0.30 and releases 0.07 in, so a 0.14 m
# rim (release at 0.19) clears with ~2 cm to spare and a 0.25 m one does not.
#
# Collides as five convex slabs (floor + four walls) from the decomposition
# beside the mesh: MuJoCo hulls every mesh collider, so a single-piece crate
# would be a solid block with nothing to drop into.
PROP = Prop(
    name="crate",
    label="📦",
    group="manipulation",
    title="Crate",
    mesh="assets/crate.obj",
    collision="pieces",
    # Half-extents of the OUTER box; the fallback primitive if the mesh ever
    # goes missing, and the reason rest_z is the half-height.
    size=(0.17, 0.17, 0.07),
    # Cardboard over the decomposed volume: heavy enough that a graze nudges
    # rather than launches it, light enough to stay a prop and not furniture.
    density=250,
    condim=4,
    friction=(1.0, 0.02, 0.001),
    rgba=(0.70, 0.55, 0.36, 1.0),
    rest_z=0.07,
    # In front of the robot, past the 0.25 m bumper, roughly where the skill
    # wants to end up parked -- so "lay out the set, run the skill" works.
    reach=(0.62, 0.0),
    # openBoxWallM, not a glb: the browser builds the same hollow shape MuJoCo
    # collides with, so the 3D view and the robot's camera agree without the
    # asset bundle having to ship a model for it.
    viewer={"openBoxWallM": 0.012, "fitSizeM": 0.34, "fitDim": "max", "origin": "center"},
)
