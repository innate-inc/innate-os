"""Real URDF-finger fixture shared by the live cloth regression tests."""

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import mujoco
import numpy as np
import sandbox._driver_pkg  # noqa: F401
from mars_sim_driver import world

ROOT = Path(__file__).resolve().parents[2]


def make_fixture(held=False, prop=None):
    if prop is None:
        spec = importlib.util.spec_from_file_location("sock_fixture_prop", ROOT / "sim/props/15_soft_sock.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        prop = module.PROP
        prop.root = ROOT / "sim/props"
    description = ROOT / "ros2_ws/src/mars_bot/mars_description"
    robot = ET.fromstring((description / "urdf/mars.urdf").read_text())
    for element in list(robot):
        if element.tag == "link" and element.get("name") not in ("link5", "link61", "link62"):
            robot.remove(element)
        elif element.tag == "joint" and element.get("name") not in ("joint6", "joint6M"):
            robot.remove(element)
    ET.SubElement(ET.SubElement(robot, "mujoco"), "compiler", discardvisual="false")
    urdf = ET.tostring(robot, encoding="unicode").replace("package://mars_description/", str(description) + "/")
    fingers = mujoco.MjSpec.from_string(urdf)
    for link in world.FINGER_LINKS:
        for geom in fingers.body(link).geoms:
            if geom.contype:
                geom.priority = 2
                geom.condim = world.FINGER_CONDIM
                geom.friction = world.FINGER_FRICTION
                geom.solref = world.FINGER_SOLREF
                geom.solimp = world.FINGER_SOLIMP
    fingers.add_exclude(bodyname1="link61", bodyname2="link62")
    # In the full robot link5 is dynamic, so MuJoCo's parent filter excludes
    # the overlapping finger hubs. The fixture welds it to a mocap body;
    # static parents are not filtered automatically. Preserve the real filter.
    for link in world.FINGER_LINKS:
        fingers.add_exclude(bodyname1="link5", bodyname2=link)
    fingers.joint("joint6").range = [world.GRIPPER_CLOSED_ON_AIR_RAD, 0.8727]
    fingers.joint("joint6M").range = [-0.8727, -world.GRIPPER_CLOSED_ON_AIR_RAD]
    for name in ("joint6", "joint6M"):
        fingers.joint(name).damping = [world.FINGER_DAMPING, 0, 0]
        fingers.joint(name).armature = world.FINGER_ARMATURE
    timestep = min(0.002, prop.max_timestep or 0.002)
    xml = f"""<mujoco><option timestep="{timestep}" integrator="implicitfast" cone="elliptic" impratio="10"/>
      <visual><global offwidth="800" offheight="640"/>
      <headlight ambient=".7 .7 .7" diffuse=".3 .3 .3"/></visual>
      <asset>{prop.assets_xml(0)}</asset><worldbody>
      <light pos="0 -1 2" diffuse=".7 .7 .7"/>
      <geom name="floor" type="plane" size="1 1 .01" rgba=".55 .5 .43 1" friction=".9 .01 .001"/>
      <body name="wrist_target" mocap="true" pos="0 0 .2" quat=".70710678 0 .70710678 0"/>
      <body name="wrist" pos="0 0 .2" quat=".70710678 0 .70710678 0">
        <freejoint name="wrist_free"/><inertial pos="0 0 0" mass=".2" diaginertia=".002 .002 .002"/>
      </body>
      {prop.body_xml(10, 10, 0, 1)}</worldbody>
      <equality><weld body1="wrist" body2="wrist_target" solref=".004 1" solimp=".999 .9999 .001"/></equality>
      </mujoco>"""
    scene = mujoco.MjSpec.from_string(xml)
    scene.attach(fingers, frame=scene.body("wrist").add_frame(), prefix="robot_")
    model = scene.compile()
    data = mujoco.MjData(model)
    world.style_robot_geoms(model)
    binding = prop.bind(model, (10, 10))
    cloth = prop.cloth_data()
    rest = cloth.get("initial_vertices", cloth["vertices"]).copy()
    cuff = rest[np.isclose(rest[:, 2], rest[:, 2].max())].mean(axis=0)
    # Lay the sock on the floor, cuff beneath the fingers, toe away from wrist.
    target = (rest - cuff) @ np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    target[:, 2] += 0.004
    target[:, 1] -= 0.010
    if held:
        target = rest - cuff
        target[:, 2] += 0.285
    data.qpos[binding._qpos_indices] = target - binding._compiled_vertices
    binding.set_active(data, True)
    # Start outside both cloth panels, not with each finger already threaded
    # inside the cuff. The servo must establish the pinch through contact.
    for name, value in (("joint6", 0.12 if held else 0.8), ("joint6M", -0.12 if held else -0.8)):
        data.joint("robot_" + name).qpos[:] = value
    if held:
        data.mocap_pos[0, 2] = 0.35
    # Mocap geoms alone have zero solver velocity: a dynamic, weld-driven
    # wrist is needed to test tangential friction during a lift faithfully.
    data.joint("wrist_free").qpos[:] = np.r_[data.mocap_pos[0], data.mocap_quat[0]]
    mujoco.mj_forward(model, data)
    if held:
        assert not any(
            binding.flex_id in c.flex and any(g > 0 for g in c.geom) and c.dist < -0.0001 for c in data.contact
        ), "Fixture starts with fingers intersecting cloth"
    return model, data, binding


def smooth(value):
    value = np.clip(value, 0, 1)
    return value * value * (3 - 2 * value)
