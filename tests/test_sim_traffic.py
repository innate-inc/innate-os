import math
import sys
from pathlib import Path

import pytest

DRIVER_SOURCE = Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
sys.path.insert(0, str(DRIVER_SOURCE))

from mars_sim_driver.traffic import (  # noqa: E402
    CAR_HALF_LENGTH_M,
    CYCLE_S,
    EW,
    GREEN,
    INTERSECTION_MAX_M,
    INTERSECTION_MIN_M,
    NS,
    RED,
    YELLOW,
    TrafficController,
    phase_at,
)


def test_signal_cycle_has_yellow_and_all_red_without_conflicting_go_aspects():
    boundaries = {
        0.0: "all_red_to_ns",
        4.0: "ns_green",
        16.0: "ns_yellow",
        19.0: "all_red_to_ew",
        23.0: "ew_green",
        35.0: "ew_yellow",
        38.0: "all_red_to_ns",
    }
    for sim_time, expected in boundaries.items():
        phase, aspects, _remaining = phase_at(sim_time)
        assert phase == expected
        assert not (aspects[NS] in {GREEN, YELLOW} and aspects[EW] in {GREEN, YELLOW})
    assert phase_at(CYCLE_S + 4.0) == phase_at(4.0)


def test_red_stops_before_the_line_and_even_late_green_can_commit_safely():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    eastbound.position = eastbound.lane.stop - 2.0
    eastbound.speed = 3.0

    # The all-red before EW green requires an ordinary physical stop.
    for _ in range(300):
        traffic.advance(0.01, 20.0)
    assert eastbound.position <= eastbound.lane.stop + 1e-9
    assert eastbound.speed == 0.0

    # Even the final instant of green releases a stopped car. It becomes
    # committed and the 3s yellow + 4s all-red clear its rear bumper before
    # the conflicting NS green at t=42.
    traffic.advance(0.01, 34.99)
    assert eastbound.committed
    for step in range(700):
        traffic.advance(0.01, 35.0 + step * 0.01)
    assert eastbound.position - 1.8 > 6.33


def test_car_committed_over_the_stop_line_clears_during_yellow():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    eastbound.position = eastbound.lane.stop + 0.10
    eastbound.speed = 1.0
    before = eastbound.position

    for step in range(100):
        traffic.advance(0.01, 35.1 + step * 0.01)

    assert eastbound.position > before + 1.0


def test_cross_traffic_holds_on_green_until_a_blocked_car_clears_the_box():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    southbound = next(car for car in traffic.cars if car.lane.id == "southbound")
    eastbound.position = 0.0
    eastbound.speed = 0.0
    eastbound.committed = True
    southbound.position = southbound.lane.stop
    southbound.speed = 0.0

    for step in range(100):
        traffic.advance(0.01, 5.0 + step * 0.01)
    assert southbound.position == southbound.lane.stop
    assert southbound.speed == 0.0

    eastbound.position = 9.0
    eastbound.committed = False
    traffic.advance(0.01, 6.0)
    assert southbound.committed
    assert southbound.position < southbound.lane.stop


def test_visible_signal_stays_red_while_conflicting_car_blocks_the_box():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    southbound = next(car for car in traffic.cars if car.lane.id == "southbound")
    eastbound.position = 0.0
    eastbound.speed = 0.0
    eastbound.committed = True
    southbound.position = southbound.lane.stop
    southbound.speed = 0.0

    # The static schedule says NS green at t=5, but an EW car still owns the
    # junction.  Lamps, published state, and car permission must all hold red.
    traffic.advance(0.01, 5.0)
    blocked = traffic.state(5.01, 0)
    assert blocked["phase"] == "ns_green_clearance_hold"
    assert blocked["signals"] == {NS: RED, EW: RED}
    assert southbound.position == southbound.lane.stop

    eastbound.position = 9.0
    eastbound.committed = False
    traffic.advance(0.01, 5.01)
    released = traffic.state(5.02, 0)
    assert released["phase"] == "ns_green"
    assert released["signals"] == {NS: GREEN, EW: RED}
    assert southbound.position < southbound.lane.stop


def test_robot_blocked_commitment_expires_before_conflicting_green():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    for car in traffic.cars:
        if car.lane.signal_group == NS:
            car.position = 10.0 if car.lane.direction > 0 else -10.0
    eastbound.position = eastbound.lane.stop
    eastbound.speed = 0.0
    robot_block = (-6.10, eastbound.lane.fixed)

    traffic.advance(0.01, 34.99, robot_xy=robot_block)
    assert eastbound.committed
    assert eastbound.position == eastbound.lane.stop
    for step in range(801):
        traffic.advance(0.01, 35.0 + step * 0.01, robot_xy=robot_block)
    assert not eastbound.committed

    # At t=43 NS is green and EW is red. Clearing MARS must not release the
    # eastbound queue until its own next green.
    for step in range(100):
        traffic.advance(0.01, 43.01 + step * 0.01, robot_xy=(0.0, 8.0))
    assert eastbound.position <= eastbound.lane.stop
    assert eastbound.speed == 0.0


def test_four_cycle_soak_never_occupies_the_box_against_conflicting_green():
    traffic = TrafficController("low-poly-town")
    for step in range(round(4 * CYCLE_S / 0.01)):
        sim_time = step * 0.01
        traffic.advance(0.01, sim_time, robot_xy=(1.6, -9.0))
        _phase, aspects, _remaining = phase_at(sim_time)
        occupied = {NS: False, EW: False}
        for car in traffic.cars:
            if (
                car.active
                and car.position + CAR_HALF_LENGTH_M > INTERSECTION_MIN_M
                and car.position - CAR_HALF_LENGTH_M < INTERSECTION_MAX_M
            ):
                occupied[car.lane.signal_group] = True
        assert not (occupied[NS] and occupied[EW])
        assert not (aspects[NS] == GREEN and occupied[EW])
        assert not (aspects[EW] == GREEN and occupied[NS])


def test_northbound_car_yields_to_mars_at_the_town_spawn():
    traffic = TrafficController("low-poly-town")
    northbound = next(car for car in traffic.cars if car.lane.id == "northbound")
    northbound.position = northbound.lane.start

    for step in range(500):
        traffic.advance(0.01, 3.0 + step * 0.01, robot_xy=(1.6, -9.0))

    required_center_gap = 1.8 + 0.28 + 0.55
    assert northbound.position <= -9.0 - required_center_gap + 1e-6
    assert math.isclose(northbound.speed, 0.0, abs_tol=1e-9)


def test_northbound_respawn_stays_off_scene_while_mars_blocks_the_approach():
    traffic = TrafficController("low-poly-town")
    northbound = next(car for car in traffic.cars if car.lane.id == "northbound")
    northbound.position = northbound.lane.end - 0.01
    northbound.speed = 2.0

    traffic.advance(0.02, 5.0, robot_xy=(1.6, -9.0))
    assert not northbound.active
    assert "northbound" not in traffic.state(5.02, 0)["cars"]

    traffic.advance(0.02, 5.02, robot_xy=(8.0, -9.0))
    assert northbound.active
    assert northbound.position == northbound.lane.start


def test_respawn_sequence_and_reset_are_deterministic():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    eastbound.position = eastbound.lane.end - 0.01
    eastbound.speed = 2.0
    traffic.advance(0.02, 20.0)

    assert eastbound.position == eastbound.lane.start
    assert eastbound.spawn_seq == 1
    traffic.reset()
    assert eastbound.position == eastbound.lane.initial_position
    assert eastbound.speed == 0.0
    assert eastbound.spawn_seq == 0


def test_respawn_waits_off_map_until_mars_clears_the_lane_start():
    traffic = TrafficController("low-poly-town")
    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    eastbound.position = eastbound.lane.end - 0.01
    eastbound.speed = 2.0
    blocked_spawn = (eastbound.lane.start, eastbound.lane.fixed)

    traffic.advance(0.02, 20.0, robot_xy=blocked_spawn)
    assert not eastbound.active
    assert "eastbound" not in traffic.state(20.02, 0)["cars"]
    assert eastbound.spawn_seq == 0

    traffic.advance(0.02, 20.02, robot_xy=blocked_spawn)
    assert not eastbound.active
    traffic.advance(0.02, 20.04, robot_xy=(0.0, 8.0))
    assert eastbound.active
    assert eastbound.position == eastbound.lane.start
    assert eastbound.spawn_seq == 1


def test_manifest_and_mjcf_share_four_procedural_cars_and_apartment_has_none():
    town = TrafficController("low-poly-town")
    xml = town.bodies_xml()
    manifest = town.manifest()

    assert xml.count('mocap="true"') == 4
    assert xml.count('contype="2" conaffinity="0"') == 8
    assert manifest is not None
    assert [car["id"] for car in manifest["cars"]] == [car.lane.id for car in town.cars]
    assert manifest["car_model"]["length"] == 3.6

    apartment = TrafficController("apartment")
    assert apartment.bodies_xml() == ""
    assert apartment.manifest() is None
    assert apartment.state(0.0, 0) is None


def test_procedural_parts_fit_the_declared_car_envelope():
    from mars_sim_driver.traffic import CAR_MODEL

    half_length = CAR_MODEL["length"] / 2
    half_width = CAR_MODEL["width"] / 2
    for part in CAR_MODEL["parts"]:
        if part["shape"] == "box":
            half = [value / 2 for value in part["size"]]
        else:
            # Every wheel rotates the descriptor's +Z cylinder axis onto Y.
            half = [part["radius"], part["length"] / 2, part["radius"]]
        assert abs(part["position"][0]) + half[0] <= half_length + 1e-9
        assert abs(part["position"][1]) + half[1] <= half_width + 1e-9
        assert part["position"][2] - half[2] >= -1e-9
        assert part["position"][2] + half[2] <= CAR_MODEL["height"] + 1e-9


def test_car_lamps_are_proud_of_body_to_avoid_coplanar_flicker():
    from mars_sim_driver.traffic import CAR_MODEL

    body = CAR_MODEL["parts"][0]
    body_front = body["position"][0] + body["size"][0] / 2
    body_rear = body["position"][0] - body["size"][0] / 2
    headlights = [part for part in CAR_MODEL["parts"] if part["material"] == "headlight"]
    taillights = [part for part in CAR_MODEL["parts"] if part["material"] == "taillight"]

    assert all(part["position"][0] + part["size"][0] / 2 > body_front + 0.01 for part in headlights)
    assert all(part["position"][0] - part["size"][0] / 2 < body_rear - 0.01 for part in taillights)


def test_mujoco_compiles_traffic_and_car_colliders_contact_only_opted_in_robot():
    mujoco = pytest.importorskip("mujoco")
    if not callable(getattr(getattr(mujoco, "MjSpec", None), "from_string", None)):
        pytest.skip("a test module installed a minimal fake mujoco without MjSpec")

    traffic = TrafficController("low-poly-town")
    materials = "\n".join(
        f'<material name="mat_signal-{group}-{aspect}" rgba="1 1 1 1"/>'
        for group in ("ns", "ew")
        for aspect in ("red", "yellow", "green")
    )
    world_xml = f"""
    <mujoco>
      <asset>{materials}</asset>
      <worldbody>
        {traffic.bodies_xml()}
      </worldbody>
    </mujoco>
    """
    robot_spec = mujoco.MjSpec.from_string(
        """
        <mujoco><worldbody><body name="base" pos="0 0 0.3">
          <freejoint/>
          <geom name="collision" type="box" size="0.3 0.3 0.3" contype="1" conaffinity="1"/>
        </body></worldbody></mujoco>
        """
    )
    traffic.configure_robot_spec(robot_spec)
    world_spec = mujoco.MjSpec.from_string(world_xml)
    world_spec.attach(robot_spec, frame=world_spec.worldbody.add_frame(), prefix="robot_")
    model = world_spec.compile()
    data = mujoco.MjData(model)
    traffic.bind(model)
    traffic.reset(data)

    assert model.nmocap == 4
    robot_geom = model.geom("robot_collision").id
    assert int(model.geom_conaffinity[robot_geom]) == 3
    assert int(model.body_conaffinity[model.body("robot_base").id]) & 2

    eastbound = next(car for car in traffic.cars if car.lane.id == "eastbound")
    data.qpos[:3] = (eastbound.position, eastbound.lane.fixed, 0.3)
    mujoco.mj_forward(model, data)
    robot_body = model.body("robot_base").id
    assert any(
        robot_body in (int(model.geom_bodyid[contact.geom1]), int(model.geom_bodyid[contact.geom2]))
        for contact in data.contact
    )

    ns_red = model.mat("mat_signal-ns-red").id
    ns_green = model.mat("mat_signal-ns-green").id
    assert tuple(model.mat_rgba[ns_red]) == (1.0, 1.0, 1.0, 1.0)
    assert tuple(model.mat_rgba[ns_green]) == (0.1, 0.1, 0.1, 1.0)
    traffic.advance(0.01, 5.0)
    assert tuple(model.mat_rgba[ns_red]) == (0.1, 0.1, 0.1, 1.0)
    assert tuple(model.mat_rgba[ns_green]) == (1.0, 1.0, 1.0, 1.0)
