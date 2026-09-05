import math
import sys
from pathlib import Path

import pytest

DRIVER_SOURCE = Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
sys.path.insert(0, str(DRIVER_SOURCE))

import mars_sim_driver.traffic as traffic_model  # noqa: E402


def _car(traffic: traffic_model.TrafficController, lane_id: str):
    return next(car for car in traffic.cars if car.lane.id == lane_id)


def _advance(
    traffic: traffic_model.TrafficController,
    start: float,
    duration: float,
    robot_xy: tuple[float, float] | None = None,
) -> None:
    for step in range(round(duration / 0.01)):
        traffic.advance(0.01, start + step * 0.01, robot_xy)


def test_crossroads_traffic_runs_safely_from_controller_through_mujoco():
    boundaries = (0.0, 4.0, 16.0, 19.0, 23.0, 35.0, 38.0)
    phases = (
        "all_red_to_ns",
        "ns_green",
        "ns_yellow",
        "all_red_to_ew",
        "ew_green",
        "ew_yellow",
        "all_red_to_ns",
    )
    assert tuple(traffic_model.phase_at(t)[0] for t in boundaries) == phases
    assert traffic_model.phase_at(traffic_model.CYCLE_S + 4.0) == traffic_model.phase_at(4.0)

    traffic = traffic_model.TrafficController("intersection")
    seen: set[str] = set()
    for step in range(round(4 * traffic_model.CYCLE_S / 0.01)):
        sim_time = step * 0.01
        traffic.advance(0.01, sim_time, (1.6, -9.0))
        state = traffic.state(sim_time, 7)
        assert state is not None
        seen.add(state["phase"])
        occupied = {
            car.lane.signal_group
            for car in traffic.cars
            if car.active
            and car.position + traffic_model.CAR_HALF_LENGTH_M > traffic_model.INTERSECTION_MIN_M
            and car.position - traffic_model.CAR_HALF_LENGTH_M < traffic_model.INTERSECTION_MAX_M
        }
        assert len(occupied) <= 1
        assert sum(aspect != traffic_model.RED for aspect in state["signals"].values()) <= 1
        assert not (state["signals"][traffic_model.NS] == traffic_model.GREEN and traffic_model.EW in occupied)
        assert not (state["signals"][traffic_model.EW] == traffic_model.GREEN and traffic_model.NS in occupied)
    assert seen == set(phases)

    # Late green still clears, while an occupied junction holds cross traffic.
    traffic = traffic_model.TrafficController("intersection")
    eastbound = _car(traffic, "eastbound")
    eastbound.position, eastbound.speed = eastbound.lane.stop - 2.0, 3.0
    _advance(traffic, 20.0, 3.0)
    traffic.advance(0.01, 34.99)
    assert eastbound.committed
    _advance(traffic, 35.0, 7.0)
    assert eastbound.position - traffic_model.CAR_HALF_LENGTH_M > traffic_model.INTERSECTION_MAX_M

    traffic = traffic_model.TrafficController("intersection")
    eastbound, southbound = _car(traffic, "eastbound"), _car(traffic, "southbound")
    eastbound.position, eastbound.speed, eastbound.committed = 0.0, 0.0, True
    southbound.position, southbound.speed = southbound.lane.stop, 0.0
    traffic.advance(0.01, 5.0)
    blocked = traffic.state(5.01, 0)
    assert blocked is not None and blocked["phase"] == "ns_green_clearance_hold"
    assert blocked["signals"] == {traffic_model.NS: traffic_model.RED, traffic_model.EW: traffic_model.RED}
    assert (southbound.position, southbound.speed) == (southbound.lane.stop, 0.0)
    eastbound.position, eastbound.committed = 9.0, False
    traffic.advance(0.01, 5.01)
    assert traffic.state(5.02, 0)["signals"][traffic_model.NS] == traffic_model.GREEN

    # Robot obstruction cancels stale commitment, enforces a follow gap, and
    # keeps a recycled actor off-map until its approach is clear.
    traffic = traffic_model.TrafficController("intersection")
    eastbound = _car(traffic, "eastbound")
    for car in traffic.cars:
        if car.lane.signal_group == traffic_model.NS:
            car.position = 10.0 if car.lane.direction > 0 else -10.0
    eastbound.position, eastbound.speed = eastbound.lane.stop, 0.0
    robot_block = (-6.10, eastbound.lane.fixed)
    traffic.advance(0.01, 34.99, robot_block)
    assert eastbound.committed and eastbound.position == eastbound.lane.stop
    _advance(traffic, 35.0, 8.01, robot_block)
    assert not eastbound.committed
    _advance(traffic, 43.01, 1.0, (0.0, 8.0))
    assert eastbound.position <= eastbound.lane.stop and eastbound.speed == 0.0

    northbound = _car(traffic, "northbound")
    northbound.position = -17.0
    _advance(traffic, 3.0, 5.0, (1.6, -9.0))
    gap = traffic_model.CAR_HALF_LENGTH_M + traffic_model.ROBOT_RADIUS_M + traffic_model.ROBOT_FOLLOW_GAP_M
    assert northbound.position <= -9.0 - gap + 1e-6 and northbound.speed == 0.0
    previous_spawn = northbound.spawn_seq
    northbound.position, northbound.speed = northbound.lane.end - 0.01, 2.0
    traffic.advance(0.02, 20.0, (1.6, -9.0))
    assert not northbound.active and "northbound" not in traffic.state(20.02, 0)["cars"]
    traffic.advance(0.02, 20.02, (8.0, -9.0))
    assert northbound.active and northbound.position == northbound.lane.start
    assert northbound.spawn_seq == previous_spawn + 1
    traffic.reset()
    assert (northbound.position, northbound.speed, northbound.spawn_seq) == (northbound.lane.initial_position, 0.0, 0)

    manifest = traffic.manifest()
    assert manifest is not None and [car["id"] for car in manifest["cars"]] == [car.lane.id for car in traffic.cars]
    assert traffic.bodies_xml().count('mocap="true"') == 4
    body = traffic_model.CAR_MODEL["parts"][0]
    body_face = max(abs(x) for x, _ in body["profile"])
    body_side = body["width"] / 2
    lamps = [part for part in traffic_model.CAR_MODEL["parts"] if part["material"] in {"headlight", "taillight"}]
    wheels = [
        part
        for part in traffic_model.CAR_MODEL["parts"]
        if part["material"] == "rubber" and part["shape"] == "cylinder"
    ]
    assert all(abs(part["position"][0]) + part["size"][0] / 2 > body_face + 0.01 for part in lamps)
    assert all(abs(part["position"][1]) + part["length"] / 2 > body_side + 0.01 for part in wheels)
    for environment_id in ("apartment", "backrooms", "low-poly-town", "custom-pack"):
        disabled = traffic_model.TrafficController(environment_id)
        assert not disabled.enabled
        assert (disabled.bodies_xml(), disabled.manifest(), disabled.state(0.0, 0)) == ("", None, None)
        assert disabled.assets_xml() == ""

    mujoco = pytest.importorskip("mujoco")
    if not callable(getattr(getattr(mujoco, "MjSpec", None), "from_string", None)):
        pytest.skip("a test module installed a minimal fake mujoco without MjSpec")
    materials = "\n".join(
        f'<material name="mat_signal-{group}-{aspect}" rgba="1 1 1 1"/>'
        for group in ("ns", "ew")
        for aspect in ("red", "yellow", "green")
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
    robot_geom, robot_body = model.geom("robot_collision").id, model.body("robot_base").id
    assert model.nmocap == 4 and int(model.geom_conaffinity[robot_geom]) == 3
    assert int(model.body_conaffinity[robot_body]) & traffic_model.CAR_COLLISION_BIT

    eastbound = _car(traffic, "eastbound")
    data.qpos[:3] = (eastbound.position, eastbound.lane.fixed, 0.3)
    mujoco.mj_forward(model, data)
    assert any(
        robot_body in (int(model.geom_bodyid[contact.geom1]), int(model.geom_bodyid[contact.geom2]))
        for contact in data.contact
    )
    # Wheels turn in the real camera/lidar geometry, not extra physics joints.
    # A quarter wheel circumference is a quarter turn on every lane heading;
    # unchanged positions keep them still and reset restores the initial angle.
    assert model.nv == 6  # only the fixture robot's free joint
    spoke_index = next(
        i
        for i, part in enumerate(traffic_model.CAR_MODEL["parts"])
        if part["shape"] == "box" and "rolling_radius" in part
    )
    for car in traffic.cars:
        spoke = model.geom(f"traffic_car_{car.lane.id}_part_{spoke_index}")
        assert spoke.contype == 0 and spoke.conaffinity == 0
        initial = spoke.quat.copy()
        car.position = 0
        traffic._write_mocap(data)
        assert spoke.quat == pytest.approx((1, 0, 0, 0))
        mujoco.mj_forward(model, data)
        unturned = data.geom_xmat[spoke.id].copy()
        car.position = car.lane.direction * 0.3 * math.pi / 2
        traffic._write_mocap(data)
        assert spoke.quat == pytest.approx((math.sqrt(0.5), 0, math.sqrt(0.5), 0))
        mujoco.mj_forward(model, data)
        pose = data.geom_xmat[spoke.id].copy()
        assert pose != pytest.approx(unturned)
        traffic._write_mocap(data)
        mujoco.mj_forward(model, data)
        assert data.geom_xmat[spoke.id] == pytest.approx(pose)
        traffic.reset(data)
        assert spoke.quat == pytest.approx(initial)
