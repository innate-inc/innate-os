"""Record one billed, real ROS pickup with a temporary data-only overlay."""

import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from websockets.sync.client import connect

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.remote_world import RemoteWorld  # noqa: E402
from mars_sim_driver.world import ARM_HOME  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("label")
parser.add_argument("--live", action="store_true")
parser.add_argument("--output-root", type=Path, required=True)
parser.add_argument("--container", required=True)
parser.add_argument("--port-base", type=int, required=True)
parser.add_argument("--max-provider-calls", type=int, default=40)
parser.add_argument("--campaign", default="development")
parser.add_argument("--scenario-id", default="onboarding-lego")
parser.add_argument("--repeat", type=int, default=0)
parser.add_argument("--prop", default="lego")
parser.add_argument("--controller", choices=("astra", "classic"), default="astra")
parser.add_argument("--yaw", type=float, default=0)
parser.add_argument("--x", type=float, default=-4.34)
parser.add_argument("--y", type=float, default=-0.47)
parser.add_argument("--prompt", default="the red LEGO brick")
parser.add_argument("--cancel-during-astra", action="store_true")
a = parser.parse_args()
if not a.live:
    parser.error("This runs robot motion in the simulator and billed inference; pass --live")
if "/" in a.label or a.label in (".", ".."):
    parser.error("Label must be a single directory name")
if not (1024 <= a.port_base <= 65500):
    parser.error("Invalid isolated simulator port range")

out = a.output_root.resolve() / a.label
out.mkdir(parents=True, exist_ok=False)
(out / "frames").mkdir()
container = a.container
shell = (
    "source /opt/ros/humble/setup.bash; source /root/innate-os/ros2_ws/install/setup.bash; "
    "export RMW_IMPLEMENTATION=rmw_zenoh_cpp ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=0 "
    'ZENOH_ROUTER_CHECK_ATTEMPTS=0; exec python3 /tmp/pickup-run-skill.py "$@"'
)


def ros(*args, **kwargs):
    return subprocess.run(["docker", "exec", container, "bash", "-c", shell, "bash", *args], **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def overlay():
    """Restore only our exact overlay; never overwrite an intervening edit."""
    skill = REPO / "workspace/innate_skills/pick_any_object.py"
    helper = skill.with_name("_pickup_probe.py")
    storage = REPO / "workspace/skill_storage/pickup_probe"
    storage.mkdir(parents=True, exist_ok=True)
    with (storage / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if helper.exists():
            raise RuntimeError("A pickup overlay already exists; inspect before continuing")
        original = skill.read_bytes()
        instrumented = (
            original
            + b"\n# Temporary benchmark overlay.\nfrom innate_skills._pickup_probe import install as _install_probe\n_install_probe(PickAnyObject)\n"
        )
        probe = Path(__file__).with_name("probe.py").read_bytes()
        (out / "original_pick_any_object.py").write_bytes(original)
        (storage / "budget.json").write_text(json.dumps({"max_calls": a.max_provider_calls}) + "\n")
        helper.write_bytes(probe)
        try:
            skill.write_bytes(instrumented)
            # Let SAS finish its file-watch debounce before action timing.
            time.sleep(3)
            yield
        finally:
            if skill.read_bytes() != instrumented or helper.read_bytes() != probe:
                raise RuntimeError("Source changed during trial; overlay retained for manual review")
            skill.write_bytes(original)
            helper.unlink()


def run():
    subprocess.run(
        ["docker", "cp", str(Path(__file__).with_name("run_skill.py")), container + ":/tmp/pickup-run-skill.py"],
        check=True,
    )
    ros("/tmp/pickup-disable.json", "--disable-only", check=True, timeout=35)
    # Confirm this is a simulator, before any reset or action submission.
    port = subprocess.check_output(
        ["docker", "exec", container, "printenv", "INNATE_WORLD_STATE_PORT"], text=True
    ).strip()
    if port != str(a.port_base + 6):
        raise RuntimeError("Container does not use the selected simulator port range")
    world = RemoteWorld("127.0.0.1", a.port_base + 5)
    # reset() restores the CURRENT servo targets, not canonical ARM_HOME.
    # Set them explicitly so the previous controller's carry pose cannot
    # change the next trial's starting arm configuration.
    world.set_joint_targets(ARM_HOME)
    world.reset()
    with connect(f"ws://127.0.0.1:{a.port_base + 6}") as ws:
        ws.send(json.dumps({"op": "drop_prop_at", "name": a.prop, "x": a.x, "y": a.y, "yaw": a.yaw}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = json.loads(ws.recv(timeout=5))
            obj = state.get("objects", {}).get(a.prop)
            if obj and abs(obj[0] - a.x) < 0.02 and abs(obj[1] - a.y) < 0.02:
                break
        else:
            raise RuntimeError("Scenario did not reset")
        time.sleep(2)
        # Drain to a fresh settled state; the reset/placement ack is not settled.
        while state.get("wall", 0) < time.time() - 0.2:
            state = json.loads(ws.recv(timeout=5))
    sources = [
        "scripts/experiments/pickup/run_trial.py",
        "scripts/experiments/pickup/run_skill.py",
        "scripts/experiments/pickup/judge.py",
        "scripts/experiments/pickup/report.py",
        "workspace/innate_skills/pick_any_object.py",
        "workspace/innate_skills/pickup_policy.py",
        "workspace/innate_skills/_pickup_probe.py",
        "workspace/innate_skills/approach.py",
        "sim/props/12_lego.py",
        "sim/props/10_cube.py",
        "ros2_ws/src/mars_bot/mars_sim_driver/mars_sim_driver/props.py",
    ]
    hashes = {relative: digest(REPO / relative) for relative in sources}
    manifest = {
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "scenario": {k: str(v) if isinstance(v, Path) else v for k, v in vars(a).items()},
        "initial_state": state,
        "speed_settings": "unchanged baseline",
        "sha256": hashes,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "source.diff").write_text(subprocess.check_output(["git", "diff"], cwd=REPO, text=True))
    for relative in sources:
        target = out / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    stop = threading.Event()
    errors = []

    def observe():
        try:
            with connect(f"ws://127.0.0.1:{a.port_base + 6}") as ws, (out / "world.jsonl").open("w") as log:
                last = 0
                while not stop.is_set():
                    sample = json.loads(ws.recv(timeout=5))
                    now = time.time()
                    if "t" in sample and now - last >= 0.08:
                        log.write(json.dumps({"observed_wall": now, **sample}) + "\n")
                        log.flush()
                        last = now
        except Exception as error:
            errors.append(type(error).__name__)

    def record():
        try:
            camera = RemoteWorld("127.0.0.1", a.port_base + 5)
            with (out / "frames.jsonl").open("w") as log:
                i = 0
                while not stop.is_set():
                    started = time.monotonic()
                    for name in ("main", "wrist"):
                        wall = time.time()
                        raw = camera.render_jpeg(name)
                        file = f"{i:06d}-{name}.jpg"
                        (out / "frames" / file).write_bytes(raw)
                        log.write(json.dumps({"file": file, "wall": wall, "camera": name}) + "\n")
                    log.flush()
                    i += 1
                    stop.wait(max(0, 0.2 - (time.monotonic() - started)))
        except Exception as error:
            errors.append(type(error).__name__)

    threads = [threading.Thread(target=f, daemon=True) for f in (observe, record)]
    for thread in threads:
        thread.start()
    remote = "/tmp/pickup-result.json"
    try:
        options = ["--cancel-during-astra"] if a.cancel_during_astra else []
        if a.controller == "classic":
            options += ["--controller", "classic"]
        with (out / "action.log").open("w") as log:
            ros(
                remote,
                "--prompt",
                a.prompt,
                *options,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=260,
                check=True,
            )
        subprocess.run(["docker", "cp", container + ":" + remote, str(out / "result.json")], check=True)
        time.sleep(22)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=6)
        with (out / "compute.txt").open("w") as log:
            subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}"], stdout=log
            )
    result = json.loads((out / "result.json").read_text())
    manifest["source_unchanged_during_trial"] = all(digest(REPO / r) == h for r, h in hashes.items())
    manifest["recording_errors"] = errors
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    events_path = REPO / "workspace/skill_storage/pickup_probe/events.jsonl"
    # A canceled data-only worker can finish after action teardown; await its
    # usage before ending accounting. This wait does not move the robot.
    deadline = time.monotonic() + 95
    while True:
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        events = [e for e in events if e["wall"] >= result["request_wall"]]
        starts = sum(e["kind"] == "provider_start" for e in events)
        ends = sum(e["kind"] == "provider_end" for e in events)
        if starts == ends or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    (out / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    usages = [e for e in events if e["kind"] in ("usage", "astra_usage") and e.get("usage")]
    if starts != len(usages):
        raise RuntimeError("Provider usage incomplete; inspect before another billed trial")
    if errors or not manifest["source_unchanged_during_trial"]:
        raise RuntimeError("Recording or source verification failed; trial invalid")


with overlay():
    run()
print(out, flush=True)
