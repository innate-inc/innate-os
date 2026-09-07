"""First-mission lifecycle and honest success judging through ChallengeEngine."""

import importlib.util
import json
import math
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_sim_driver"))
from mars_sim_driver.challenges import ChallengeEngine  # noqa: E402


class Scene:
    def __init__(self, environment):
        self.environment = SimpleNamespace(id=environment)
        self.data = SimpleNamespace(time=0.0)
        self.resets = 0
        self.drops = []

    def reset(self, *, spawn=None):
        self.resets += 1
        self.data.time = 0
        self.spawn = spawn

    def drop_prop_at(self, *args):
        self.drops.append(args)
        return True


def engine(tmp_path, environment):
    scene = Scene(environment)
    result = ChallengeEngine(
        scene, threading.Lock(), roots=[ROOT / "sim/challenges"], progress_path=tmp_path / "progress.json"
    )
    return result, scene


def test_first_mission_reconnect_and_skip_belong_to_exact_attempt(tmp_path):
    e, scene = engine(tmp_path, "apartment")
    token = str(uuid.uuid4())
    assert not e.start("way_out", token)
    assert not e.start("put_it_away", "malformed")
    assert scene.resets == 0
    assert e.start("put_it_away", token)
    assert e.start("put_it_away", token)
    assert scene.resets == 1 and len(scene.drops) == 2
    assert e.progress["put_it_away"]["attempts"] == 1
    context = json.loads((tmp_path / "challenge_context.json").read_text())
    assert context["attempt_id"] == token and "ThrowObject" in context["guidance"]
    e.abort(str(uuid.uuid4()))
    assert e.active is not None
    e.abort(token)
    assert e.active is None
    assert json.loads((tmp_path / "challenge_context.json").read_text()) is None


def test_challenges_and_saved_results_belong_to_their_environment(tmp_path):
    e, scene = engine(tmp_path, "apartment")
    assert all(c.environments for c in e.challenges.values())
    assert "put_it_away" in {c["id"] for c in e.roster()}
    e._record("put_it_away", "passed", 12.0)
    # A newly loaded engine gets the saved scene result without browser state.
    e, scene = engine(tmp_path, "apartment")
    assert e._block(None)["progress"]["put_it_away"]["passed"]
    for environment, expected in [("backrooms", "way_out"), ("intersection", "other_side")]:
        scene.environment.id = environment
        assert [c["id"] for c in e.roster()] == [expected]
        assert "put_it_away" not in e._block(None)["progress"]
        assert not e.start("put_it_away")
    scene.environment.id = "apartment"
    assert e._block(None)["progress"]["put_it_away"]["passed"]


def test_cleanup_requires_brick_settled_below_rim_and_survives_misses(tmp_path):
    e, _ = engine(tmp_path, "apartment")
    assert e.start("put_it_away")
    box = [0, 0, 0.07, 1, 0, 0, 0]

    def tick(t, xyz):
        return e.tick(t, (0, 0, 0), {}, e.world_epoch, objects={"crate": box, "lego": [*xyz, 1, 0, 0, 0]})["active"]

    # The gripper hovering above the box and a brick beside it cannot pass.
    assert tick(1, [0, 0, 0.3])["state"] == "running"
    assert tick(4, [0.18, 0, 0.04])["state"] == "running"
    assert tick(5, [0, 0, 0.04])["state"] == "running"
    assert tick(6, [0.18, 0, 0.04])["state"] == "running"
    assert tick(7, [0, 0, 0.04])["state"] == "running"
    assert tick(9.1, [0, 0, 0.04])["state"] == "passed"
    assert json.loads((tmp_path / "progress.json").read_text())["challenges"]["put_it_away"]["passed"]
    # A tipped box is judged in all three local axes, not its old floor square.
    assert e.start("put_it_away")
    box[3:] = [math.sqrt(0.5), math.sqrt(0.5), 0, 0]
    assert tick(1, [0, 0.12, 0.07])["state"] == "running"
    assert tick(4, [0, 0.12, 0.07])["state"] == "running"
    assert tick(5, [0, 0.03, 0.07])["state"] == "running"
    assert tick(7.1, [0, 0.03, 0.07])["state"] == "passed"


def test_crossing_must_use_crosswalk_and_retry_from_curb_after_contact(tmp_path):
    e, scene = engine(tmp_path, "intersection")
    assert e.start("other_side")
    assert scene.spawn == (5.2, -5.3, 180.0)

    def tick(x, y=-5.3, contact=False):
        return e.tick(1, (x, y, 0), {}, e.world_epoch, traffic_contact=contact)["active"]["state"]

    assert tick(-5.2) == "running"  # merely appearing at the destination is not a crossing
    assert tick(5.2) == "running"
    assert tick(0, contact=True) == "running"
    assert tick(-5.2) == "running"
    assert tick(5.2) == "running"
    assert tick(0, y=-7) == "running"  # detour outside the stripes invalidates the crossing
    assert tick(-5.2) == "running"
    assert tick(5.2) == "running"
    assert tick(0) == "running"
    assert tick(-5.2) == "passed"


def test_exit_only_passes_in_exit_zone(tmp_path):
    e, _ = engine(tmp_path, "backrooms")
    assert e.start("way_out")
    for t, pose in [(1, (-30.95, -5.2, 0)), (2, (3.2, -5.2, 0))]:
        assert e.tick(t, pose, {}, e.world_epoch)["active"]["state"] == "running"
    assert e.tick(3.1, (3.2, -5.2, 0), {}, e.world_epoch)["active"]["state"] == "passed"


def test_seeded_views_load_only_on_the_right_new_map_and_do_not_return_after_clear(tmp_path):
    path = ROOT / "ros2_ws/src/brain/brain_client/brain_client/memory/store.py"
    spec = importlib.util.spec_from_file_location("first_mission_memory_store", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    maps = tmp_path / "data/maps"
    maps.mkdir(parents=True)
    (maps / "room.pgm").write_bytes(b"map content")
    seed = tmp_path / "seeds/room"
    seed.mkdir(parents=True)
    (seed / "1.jpg").write_bytes(b"\xff\xd8image")
    import hashlib

    index = dict(
        version=1,
        fingerprint=hashlib.sha256(b"map content").hexdigest(),
        memories=[dict(id=1, x=1.5, y=2.0, theta=0.0, stamp=123.0, label="The cleanup box")],
    )
    (seed / "index.json").write_text(json.dumps(index))
    store = module.MemoryStore(tmp_path / "data", seed_dir=tmp_path / "seeds")
    store.switch_map("room.yaml")
    assert store.snapshot().memories[0].label == "The cleanup box"
    assert store.image_path(1).read_bytes() == b"\xff\xd8image"
    store.add(3, 4, 0, 456, b"\xff\xd8new recording")
    restored = module.MemoryStore(tmp_path / "data", seed_dir=tmp_path / "seeds")
    restored.switch_map("room.yaml")
    assert len(restored.snapshot().memories) == 2  # seed does not replace recordings on reload
    restored.clear()
    restored = module.MemoryStore(tmp_path / "data", seed_dir=tmp_path / "seeds")
    restored.switch_map("room.yaml")
    assert restored.snapshot().memories == ()
    (maps / "room.pgm").write_bytes(b"different map frame")
    restored.switch_map("room.yaml")
    assert restored.snapshot().memories == ()  # old authored coordinates are refused
