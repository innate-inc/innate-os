"""Towels from the Blaze map: a low stack of three folded towels (mesh from sim/tools/build_benchmark_props.py)."""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="blaze_towels",
    label="🧺",
    title="Folded towels",
    mesh="../objects/blaze_towels.obj",
    viewer={"glb": "/models/blaze/blaze_towels.glb", "preNormalized": True},
    collision="box",
    size=(0.075, 0.05, 0.045),
    rgba=(0.498, 0.635, 0.604, 1.0),
    initial_pose=(-2.6, -1.4, 0),
    rest_z=0.045,
    drop_z=0.057,
)
