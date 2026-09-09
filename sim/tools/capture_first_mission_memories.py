"""Capture authored first-mission views from the real simulator, with map identity.

Run with the sim Python environment after installing/building the world assets.
This is an offline asset authoring tool; it never connects to a running robot.
"""

import hashlib
import json
import math
import sys
import tempfile
import threading
import time
from pathlib import Path

import mujoco

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.challenges import ChallengeEngine  # noqa: E402
from mars_sim_driver.core import VirtualMars  # noqa: E402
from mars_sim_driver.environments import Environment  # noqa: E402

VIEWS = {"backrooms": ("way_out", [(-16.3, -5.2, 0, "Green exit down the Backrooms corridor")])}


def main() -> None:
    for name, (challenge_id, views) in VIEWS.items():
        environment = Environment.load(name)
        sim = VirtualMars(environment=environment, render_wh=(640, 480))
        try:
            with tempfile.TemporaryDirectory() as directory:
                engine = ChallengeEngine(sim, threading.Lock(), progress_path=Path(directory) / "progress.json")
                if not engine.start(challenge_id):
                    raise SystemExit(f"{challenge_id} did not start in {name}")
                sim.step(0.5)
                rows = []
                output = ROOT / "workspace/innate_agents/intro_memories" / Path(environment.map_name).stem
                output.mkdir(parents=True, exist_ok=True)
                for number, (x, y, degrees, label) in enumerate(views, 1):
                    for axis, value in (("x", x), ("y", y), ("yaw", math.radians(degrees))):
                        sim.data.qpos[sim.model.joint(f"robot_base_{axis}").qposadr[0]] = value
                    sim.data.qvel[:] = 0
                    sim._hold = None
                    sim.set_joint_target("joint_head", 0.0)
                    mujoco.mj_forward(sim.model, sim.data)
                    sim.step(0.4)
                    jpeg = sim.render_jpeg("main")
                    (output / f"{number}.jpg").write_bytes(jpeg)
                    actual_x, actual_y, actual_yaw = sim.pose()
                    rows.append(
                        dict(id=number, x=actual_x, y=actual_y, theta=actual_yaw, stamp=time.time(), label=label)
                    )
                map_path = ROOT / "sim/assets/map" / Path(environment.map_name).with_suffix(".pgm")
                index = dict(version=1, fingerprint=hashlib.sha256(map_path.read_bytes()).hexdigest(), memories=rows)
                (output / "index.json").write_text(json.dumps(index, indent=2) + "\n")
                print(f"Captured {len(rows)} {name} memories", flush=True)
        finally:
            sim.close()


if __name__ == "__main__":
    main()
