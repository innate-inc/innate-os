"""A labeled document box with a 50 mm grasp axis and matching visual mesh.

Staged on the kitchen floor so the current floor-grasp skill can attempt it.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="blaze_documents",
    label="📦",
    title="Document box",
    mesh="../objects/blaze_documents.obj",
    viewer={"glb": "/models/blaze/blaze_documents.glb", "preNormalized": True},
    collision="box",
    size=(0.0575, 0.0250, 0.0925),
    # Blue, not (0.9333, 0.9059, 0.8471) -- which is BYTE-IDENTICAL to the
    # blaze wall colour: a wall-coloured box on a shelf is invisible by
    # construction, and the probe agent that searched for it never saw box OR
    # shelf. Distinct from the red medicine and green towels. Post-dates the
    # Aug 16 live run.
    rgba=(0.20, 0.35, 0.70, 1.0),
    initial_pose=(-2.55, 1.2, 0),
    rest_z=0.0925,
    drop_z=0.105,
)
