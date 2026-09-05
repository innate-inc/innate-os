"""Car/robot contacts must survive MuJoCo's compile-time mask aggregation."""

import sys
from pathlib import Path

import mujoco

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.traffic import SIGNAL_MATERIALS, TrafficController  # noqa: E402


def test_robot_collides_with_car():
    traffic = TrafficController(enabled=True)
    materials = "".join(
        f'<material name="mat_{name.lower().replace("_", "-")}" rgba="1 1 1 1"/>'
        for aspects in SIGNAL_MATERIALS.values()
        for name in aspects.values()
    )
    world = mujoco.MjSpec.from_string(
        f"<mujoco><asset>{materials}{traffic.assets_xml()}</asset><worldbody>{traffic.bodies_xml()}</worldbody></mujoco>"
    )
    robot = mujoco.MjSpec.from_string(
        '<mujoco><worldbody><body name="base" pos="0 0 0.3"><freejoint/>'
        '<geom name="collision" type="box" size="0.3 0.3 0.3" contype="1" conaffinity="1"/>'
        "</body></worldbody></mujoco>"
    )
    traffic.configure_robot_spec(robot)
    world.attach(robot, frame=world.worldbody.add_frame(), prefix="robot_")
    model = world.compile()
    data = mujoco.MjData(model)
    traffic.bind(model)
    traffic.reset(data)
    car = traffic.cars[0]
    data.qpos[:3] = (car.position, car.lane.fixed, 0.3)
    mujoco.mj_forward(model, data)
    assert any(model.geom("robot_collision").id in (contact.geom1, contact.geom2) for contact in data.contact)
