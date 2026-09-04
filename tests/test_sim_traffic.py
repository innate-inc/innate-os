import fcntl
import hashlib
import json
import sys
from pathlib import Path

import pytest
import trimesh
from sim.tools import build_low_poly_town as town_builder

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


def _files(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_town_build_is_grounded_phase_isolated_and_transactional(tmp_path, monkeypatch):
    source = ["mtllib town.mtl", "o Decorative_Bush", "usemtl Red", "f 1 2 3"]
    for name in town_builder.SIGNAL_OBJECT_PHASE:
        source.append(f"o {name}")
        for aspect in town_builder.SIGNAL_ASPECTS:
            source.extend((f"usemtl {aspect}", "f 1 2 3"))
    source_text = "\n".join(source) + "\n"
    mtl = "newmtl Red\nKd 1 0 0\nnewmtl Yellow\nKd 1 1 0\nnewmtl Green\nKd 0 1 0\n"
    obj, rewritten_mtl = town_builder.isolate_signal_materials(source_text, mtl)
    assert "o Decorative_Bush\nusemtl Red" in obj
    assert obj.count("usemtl Signal_NS_Red") == obj.count("usemtl Signal_EW_Green") == 6
    assert "newmtl Signal_NS_Yellow\nKd 1 1 0" in rewritten_mtl
    with pytest.raises(RuntimeError, match="unexpected traffic-light material assignments"):
        town_builder.isolate_signal_materials(source_text.replace("usemtl Green", "usemtl Gray", 1), mtl)

    sim = tmp_path / "sim"
    assets = sim / "assets/local-environments/low-poly-town"
    viewer = sim / "viewer/local-environments/low-poly-town"
    manifest = sim / "environments.local/low-poly-town/manifest.json"
    for target in (assets, viewer):
        target.mkdir(parents=True)
        (target / "last-working").write_text(target.as_posix(), encoding="utf-8")
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"last_working": true}\n', encoding="utf-8")
    source_obj, source_mtl = tmp_path / "town.obj", tmp_path / "town.mtl"
    source_obj.write_text("licensed source", encoding="utf-8")
    source_mtl.write_text("licensed materials", encoding="utf-8")
    monkeypatch.setattr(town_builder, "SIM", sim)

    lock_path = sim / "environments.local/.low-poly-town.build.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            town_builder.build(source_obj, source_mtl)

    with pytest.raises(FileNotFoundError, match="source is not installed"):
        town_builder.build(tmp_path / "missing.obj", source_mtl)
    assert (assets / "last-working").is_file() and (viewer / "last-working").is_file()
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"last_working": True}

    generation = "first"
    generated_scenes: list[trimesh.Scene] = []

    def grounded_scene(*_args):
        base_top = town_builder.BASE_SLAB_SOURCE_TOP_Y - town_builder.ROAD_SURFACE_Y
        base = trimesh.creation.box(extents=(30.0, 0.1, 30.0))
        base.apply_translation((0.0, base_top - 0.05, 0.0))
        base.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.SimpleMaterial(name="Gray"))
        marking = trimesh.Trimesh(
            vertices=((-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 0.0, 1.0), (-1.0, 0.0, 1.0)),
            faces=((0, 1, 2), (0, 2, 3)),
            process=False,
        )
        marking.visual = trimesh.visual.TextureVisuals(material=trimesh.visual.material.SimpleMaterial(name="White"))
        scene = trimesh.Scene({"base": base, "marking": marking})
        generated_scenes.append(scene)
        return scene

    def write_browser(_scene, root):
        (root / "scene.glb").write_text(generation, encoding="utf-8")

    def write_visuals(_scene, root):
        room = root / "road"
        room.mkdir()
        for name in ("road.obj", "road.png"):
            (room / name).write_text(generation, encoding="utf-8")
        return 1

    def write_collisions(root, viewer_root):
        room = root / "town"
        room.mkdir()
        (room / "town_collision_000.obj").write_text(generation, encoding="utf-8")
        (viewer_root / "hulls.f32").write_text(generation, encoding="utf-8")
        (viewer_root / "manifest.json").write_text("[]\n", encoding="utf-8")
        return 1

    def write_map(root):
        (root / "town.pgm").write_text(generation, encoding="utf-8")
        (root / "town.yaml").write_text("image: town.pgm\n", encoding="utf-8")
        return 1, 1

    monkeypatch.setattr(town_builder, "_load_authored_scene", grounded_scene)
    monkeypatch.setattr(town_builder, "_apply_flat_shading", lambda _scene: None)
    monkeypatch.setattr(town_builder, "_prepare_daylight_materials", lambda _scene: None)
    monkeypatch.setattr(town_builder, "_write_browser_scene", write_browser)
    monkeypatch.setattr(town_builder, "_write_mujoco_visuals", write_visuals)
    monkeypatch.setattr(town_builder, "_write_collision_proxies", write_collisions)
    monkeypatch.setattr(town_builder, "_write_navigation_map", write_map)

    town_builder.build(source_obj, source_mtl)
    assert (assets / "visual/road/road.obj").read_text(encoding="utf-8") == generation
    assert (viewer / "scene.glb").read_text(encoding="utf-8") == generation
    attribution = json.loads(manifest.read_text(encoding="utf-8"))["attribution"]
    assert attribution["source_sha256"] == hashlib.sha256(source_obj.read_bytes()).hexdigest()
    assert attribution["generated_assets_sha256"] == town_builder._tree_digest(assets, viewer)
    scene = generated_scenes[-1]
    assert scene.geometry["marking"].bounds[:, 1] == pytest.approx((0.002, 0.002))
    assert scene.geometry["base"].bounds[1, 1] == pytest.approx(
        town_builder.BASE_SLAB_SOURCE_TOP_Y - town_builder.ROAD_SURFACE_Y - town_builder.BASE_SLAB_CLEARANCE
    )
    assert town_builder._collision_proxies()[0][1].bounds[1, 1] == pytest.approx(0.10)

    previous = (_files(assets), _files(viewer), manifest.read_bytes())
    generation = "second"
    real_replace = Path.replace

    def fail_viewer_publish(path, target):
        if path.name.startswith(".low-poly-town.build-") and Path(target) == viewer:
            raise OSError("forced viewer publish failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_viewer_publish)
    with pytest.raises(OSError, match="forced viewer publish failure"):
        town_builder.build(source_obj, source_mtl)
    assert (_files(assets), _files(viewer), manifest.read_bytes()) == previous
    assert not list(sim.rglob(".low-poly-town.build-*"))
    assert not list(sim.rglob(".low-poly-town.backup-*"))


def test_town_traffic_runs_safely_from_controller_through_mujoco():
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

    traffic = traffic_model.TrafficController("low-poly-town")
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
    traffic = traffic_model.TrafficController("low-poly-town")
    eastbound = _car(traffic, "eastbound")
    eastbound.position, eastbound.speed = eastbound.lane.stop - 2.0, 3.0
    _advance(traffic, 20.0, 3.0)
    traffic.advance(0.01, 34.99)
    assert eastbound.committed
    _advance(traffic, 35.0, 7.0)
    assert eastbound.position - traffic_model.CAR_HALF_LENGTH_M > traffic_model.INTERSECTION_MAX_M

    traffic = traffic_model.TrafficController("low-poly-town")
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
    traffic = traffic_model.TrafficController("low-poly-town")
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
    northbound.position = northbound.lane.start
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
    body_face = abs(body["position"][0]) + body["size"][0] / 2
    lamps = [part for part in traffic_model.CAR_MODEL["parts"] if part["material"] in {"headlight", "taillight"}]
    assert all(abs(part["position"][0]) + part["size"][0] / 2 > body_face + 0.01 for part in lamps)
    apartment = traffic_model.TrafficController("apartment")
    assert (apartment.bodies_xml(), apartment.manifest(), apartment.state(0.0, 0)) == ("", None, None)

    mujoco = pytest.importorskip("mujoco")
    if not callable(getattr(getattr(mujoco, "MjSpec", None), "from_string", None)):
        pytest.skip("a test module installed a minimal fake mujoco without MjSpec")
    materials = "\n".join(
        f'<material name="mat_signal-{group}-{aspect}" rgba="1 1 1 1"/>'
        for group in ("ns", "ew")
        for aspect in ("red", "yellow", "green")
    )
    world = mujoco.MjSpec.from_string(
        f"<mujoco><asset>{materials}</asset><worldbody>{traffic.bodies_xml()}</worldbody></mujoco>"
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
    ns_red, ns_green = model.mat("mat_signal-ns-red").id, model.mat("mat_signal-ns-green").id
    assert tuple(model.mat_rgba[ns_red]) == (1.0, 1.0, 1.0, 1.0)
    assert tuple(model.mat_rgba[ns_green]) == (0.1, 0.1, 0.1, 1.0)
    traffic.advance(0.01, 5.0)
    assert tuple(model.mat_rgba[ns_red]) == (0.1, 0.1, 0.1, 1.0)
    assert tuple(model.mat_rgba[ns_green]) == (1.0, 1.0, 1.0, 1.0)
