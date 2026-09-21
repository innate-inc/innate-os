"""Full VirtualMars apartment smoke/benchmark; never starts or stops services."""

import argparse
import json
import os
import struct
import subprocess
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ["INNATE_SIM_CLOTH_BACKEND"] = "xpbd"

import _driver_pkg  # noqa: E402,F401
import mujoco
import numpy as np
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.world_server import WorldServer
from PIL import Image


def run(output, video=True, prop_name="soft_sock"):
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    sim = VirtualMars(render_wh=(320, 240))
    setup = time.perf_counter() - started
    binding = sim.props._soft[prop_name]
    cloth_backend = sim._cloth.cloths[prop_name]
    server = WorldServer(sim)
    # Existing user-facing prop operation, not a separate cloth scene.
    assert sim.place_prop_at_robot(prop_name)
    before = binding.vertices(sim.data).copy()
    sim.step(0.2)
    assert not np.array_equal(before, binding.vertices(sim.data))
    sim.remove_prop(prop_name)
    sim.step(0.01)
    assert not sim.deformable_frames()
    sim.reset()
    assert not sim.deformable_frames()

    # Put the actual arm in a repeatable open-jaw test pose. The sock starts
    # between the jaws, just like the isolated fixture; this is NOT a floor
    # search/pickup test. Once released, neither cloth nor robot is kinematic.
    # Stay outside the robot's joint2 chassis-guard region for the entire lift.
    pose = dict(joint1=1.4, joint2=-0.23, joint3=0.08, joint4=1.72, joint5=0.0, joint6=0.18)
    for name, value in pose.items():
        sim.set_joint_target(name, value)
        sim.data.qpos[sim._joints[name][0]] = value
    sim.data.qpos[sim._mimic[0]] = -0.18
    mujoco.mj_forward(sim.model, sim.data)
    sim.step(1.0)
    assert sim.place_prop_at_robot(prop_name)
    cloth = binding.prop.cloth_data()
    initial = cloth.get("initial_vertices", cloth["vertices"]).copy()
    # This arm cannot suspend an unfolded 20 cm sock vertically from its cuff
    # above the floor. Start with the sock folded in half between the jaws,
    # never with its toe penetrating the floor (which would launch it upward).
    fold = (initial[:, 2].max() + initial[:, 2].min()) / 2
    # Round the crease, including its mid-row. A discontinuous reflection
    # leaves the front/back mid-row in reversed order and starts intersecting.
    radius = 0.003
    s, y = initial[:, 2] - fold, initial[:, 1].copy()
    angle = np.clip((s + np.pi * radius / 2) / radius, 0, np.pi)
    initial[:, 1] = radius * (1 - np.cos(angle)) + y * np.cos(angle)
    initial[:, 2] = np.where(
        abs(s) <= np.pi * radius / 2,
        fold - np.pi * radius / 2 + radius * np.sin(angle) - y * np.sin(angle),
        fold - abs(s),
    )
    from audit_cloth_crossings import count_crossings

    assert count_crossings(initial, cloth["faces"]) == 0, "Invalid benchmark: starting cloth self-intersects"
    cuff = initial[np.isclose(initial[:, 2], initial[:, 2].max())].mean(0)
    wrist = sim.model.body("robot_link5").id
    # Fixture wrist has Ry(pi/2); sock cuff sits 65 mm below its origin.
    fixture_rotation = np.array([[0.0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    local = (initial - cuff + [0, 0, -0.065]) @ fixture_rotation
    initial = local @ sim.data.xmat[wrist].reshape(3, 3).T + sim.data.xpos[wrist]
    sim.data.qpos[binding._qpos_indices] = binding._qpos0 + initial - binding._compiled_vertices
    binding._zero_motion(sim.data)
    mujoco.mj_forward(sim.model, sim.data)
    cloth_backend._near_hulls(initial)
    overlaps = []
    for g, _vertices, planes, _affine, _section in cloth_backend.contact.hulls:
        local_points = (initial - sim.data.geom_xpos[g]) @ sim.data.geom_xmat[g].reshape(3, 3)
        distance = (local_points @ planes[:, :3].T + planes[:, 3]).max(axis=1).min()
        if distance < -0.0001:
            overlaps.append((sim.model.geom(g).name, float(distance)))
    print("Initial cloth/collider overlaps:", overlaps, flush=True)
    assert not overlaps, "Invalid benchmark setup: cloth begins inside a rigid collider"
    renderer, encoder = None, None
    camera = mujoco.MjvCamera()
    camera.lookat[:] = initial.mean(0) + [0, 0, 0.02]
    camera.distance, camera.azimuth, camera.elevation = 0.55, 130, -20
    if video:
        renderer = mujoco.Renderer(sim.model, 480, 640)
        encoder = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-vcodec",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                "640x480",
                "-r",
                "30",
                "-i",
                "-",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                str(output / "apartment-sock.mp4"),
            ],
            stdin=subprocess.PIPE,
        )
    samples, physics_times = [], []
    contact_counts = []
    trajectory = []
    start_sim = sim.data.time
    start_wall = time.perf_counter()
    try:
        for frame in range(180):
            t = frame / 30
            lift = np.clip((t - 1) / 1.0, 0, 1)
            lift = lift * lift * (3 - 2 * lift)
            sim.set_joint_target("joint1", pose["joint1"] - 0.2 * lift)
            sim.set_joint_target("joint6", 0.8 if t >= 4.5 else -0.6)
            tick = time.perf_counter()
            sim.step(max(0.0, start_sim + (frame + 1) / 30 - sim.data.time))
            physics_times.append(time.perf_counter() - tick)
            contact_counts.append(cloth_backend.last_contact_stats["candidates"])
            server.publish_state()
            packet = server.state_deformables[0]
            magic, _, stamp, count, _ = struct.unpack("<4sIdII", packet[:24])
            assert magic == b"IDF1" and count == len(initial) and stamp == sim.data.time
            np.testing.assert_allclose(
                np.frombuffer(packet[24:], "<f4").reshape(-1, 3), binding.vertices(sim.data), atol=1e-6
            )
            points = binding.vertices(sim.data)
            trajectory.append(points.copy())
            assert np.isfinite(points).all()
            samples.append([sim.data.time - start_sim, *points.mean(0), points[:, 2].min()])
            if renderer:
                renderer.update_scene(sim.data, camera=camera)
                rgb = renderer.render()
                encoder.stdin.write(rgb.tobytes())
                if frame in (0, 59, 119, 179):
                    Image.fromarray(rgb).save(output / f"frame-{frame:03}.png")
    finally:
        if encoder:
            encoder.stdin.close()
            if encoder.wait() != 0:
                raise RuntimeError("Video encoder failed")
        if renderer:
            renderer.close()
    elapsed = time.perf_counter() - start_wall
    heights = np.asarray(samples)
    np.savez_compressed(output / "trajectory.npz", vertices=np.asarray(trajectory), faces=cloth["faces"])
    assert heights[(heights[:, 0] > 1) & (heights[:, 0] < 4.4), 3].min() > 0.04, "Sock was lost before release"
    assert heights[-1, 3] < 0.03, "Sock failed to fall after opening"
    sim.remove_all_props()
    server.publish_state()
    assert not server.state_deformables
    sim.reset()
    assert sim.place_prop_at_robot(prop_name)
    sim.step(0.02)
    assert np.isfinite(binding.vertices(sim.data)).all()
    result = dict(
        max_contact_candidates=max(contact_counts),
        prop=prop_name,
        backend=sim.cloth_backend,
        geoms=sim.model.ngeom,
        dofs=sim.model.nv,
        setup_seconds=setup,
        sim_seconds=6,
        loop_wall_seconds=elapsed,
        physics_seconds=sum(physics_times),
        frame_physics_p95_ms=float(np.percentile(physics_times, 95) * 1000),
        lifecycle_and_stream_checks=True,
        hold_move_release_passed=True,
        samples=samples,
    )
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2), flush=True)
    sim.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--prop", default="soft_sock", choices=("soft_sock", "wool_sock"))
    args = parser.parse_args()
    run(args.output, not args.no_video, args.prop)
