"""A benchmark world loaded as an environment pack is the benchmark's world.

Two paths build the same map: the harness through VIRTUAL_MARS_ASSETS (one
bundle per process), the localhost stack through `--environment <name>`
(the pack's manifest). If they diverged, what a person plays in the web app
would not be what the judge scored. So the pack path has to yield the same
rooms, props, spawn and challenge roster -- and the roster has to be the
bundle's alone, because an apartment scenario offered in a 9 m authored room
puts its props inside walls with goals that can never fire.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

from mars_sim_driver import core as _core  # noqa: E402
from mars_sim_driver import world as _world  # noqa: E402
from mars_sim_driver.challenges import Challenge, ChallengeEngine, load_challenges  # noqa: E402
from mars_sim_driver.environments import Environment  # noqa: E402
from runner import sources  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "sim" / "assets"
BUNDLE = "counter"


def _build(monkeypatch, assets: Path | None, environment: Environment | None):
    if assets is None:
        monkeypatch.delenv("VIRTUAL_MARS_ASSETS", raising=False)
    else:
        monkeypatch.setenv("VIRTUAL_MARS_ASSETS", str(assets))
    # core.ASSETS_DIR is bound at import (see runner.py); rebind it the way the
    # runner does so this process can build both worlds.
    monkeypatch.setattr(_core, "ASSETS_DIR", _world.default_assets_dir())
    mars = _core.VirtualMars(render_wh=(64, 48), depth_render_wh=(64, 48), environment=environment)
    try:
        return mars.room_manifest(), sorted(mars.props.props), tuple(mars._spawn)
    finally:
        mars.close()


def test_pack_world_is_the_bundle_world(monkeypatch):
    via_assets = _build(monkeypatch, REPO / "sim" / "bundles" / BUNDLE, None)
    via_pack = _build(monkeypatch, None, Environment.load(BUNDLE, ASSETS))
    assert via_pack == via_assets
    rooms, props, spawn = via_pack
    assert [room["name"] for room in rooms] == [BUNDLE]
    assert any(name.startswith(f"{BUNDLE}_") for name in props), props
    assert spawn == Environment.load(BUNDLE, ASSETS).spawn


def _sim(environment):
    return SimpleNamespace(data=SimpleNamespace(time=0.0), environment=environment)


def _challenge(cid: str, environments=None) -> Challenge:
    return Challenge(id=cid, title=cid, brief=cid, setup=[], goals=[], environments=environments)


def test_a_bundle_pack_owns_its_roster(tmp_path):
    apartment = Environment.load("apartment", ASSETS)
    pack = Environment.load(BUNDLE, ASSETS)
    untagged = _challenge("victory_lap")  # upstream leaves it for any world
    apartment_only = _challenge("shepherd", ("apartment",))
    bundle_own = _challenge("within_reach", (BUNDLE,))

    engine = ChallengeEngine(_sim(apartment), threading.Lock(), roots=[], progress_path=tmp_path / "p.json")
    engine.challenges = {c.id: c for c in (untagged, apartment_only, bundle_own)}
    assert [c["id"] for c in engine.roster()] == ["victory_lap", "shepherd"]

    engine.sim.environment = pack  # what switch_environment does
    assert [c["id"] for c in engine.roster()] == ["within_reach"]
    assert not engine.start("victory_lap")
    assert not engine.start("shepherd")

    engine.sim.environment = None  # a sim that predates packs
    assert [c["id"] for c in engine.roster()] == ["victory_lap"]


def test_an_asset_bundle_world_keeps_packs_out(tmp_path, monkeypatch):
    """The live benchmark: VIRTUAL_MARS_ASSETS names one bundle, the world
    server still runs the default (apartment) manifest, and the bundle's
    challenges must be offered exactly as they were before packs existed --
    a tagged copy from the pack would be refused under that manifest."""
    bundle = REPO / "sim" / "bundles" / BUNDLE
    monkeypatch.setenv("VIRTUAL_MARS_ASSETS", str(bundle))
    packs = [Environment.load(name, ASSETS) for name in sources() if name != "apartment"]
    apartment = Environment.load("apartment", bundle)
    engine = ChallengeEngine(_sim(apartment), threading.Lock(), progress_path=tmp_path / "p.json", packs=packs)
    expected = set(load_challenges([bundle / "challenges"]))
    assert set(engine.challenges) == expected
    assert all(c.environments is None for c in engine.challenges.values())
    assert {c["id"] for c in engine.roster()} == expected


def test_pack_challenges_load_tagged_to_their_pack(tmp_path):
    bundles = {name: root for name, (assets, root) in sources().items() if assets is not None}
    packs = [Environment.load(name, ASSETS) for name in bundles]
    engine = ChallengeEngine(_sim(packs[0]), threading.Lock(), roots=[], progress_path=tmp_path / "p.json", packs=packs)
    # Every bundle challenge is loaded, tagged to its own pack and nothing else.
    assert all(c.environments is not None for c in engine.challenges.values())
    for pack in packs:
        expected = set(load_challenges([bundles[pack.id]]))
        assert expected, pack.id
        tagged = {c.id for c in engine.challenges.values() if c.environments == (pack.id,)}
        assert tagged == expected, pack.id
        engine.sim.environment = pack
        assert {c["id"] for c in engine.roster()} == expected, pack.id


def test_a_web_app_run_hears_its_cues(tmp_path):
    """Started from the web app, a scripted challenge's narrator lines reach
    the robot through the chat outbox; started by a runner that installed a
    sink, or by the oracle, they do not."""
    from types import SimpleNamespace

    packs = [Environment.load(BUNDLE, ASSETS)]
    engine = ChallengeEngine(_sim(packs[0]), threading.Lock(), roots=[], progress_path=tmp_path / "p.json", packs=packs)
    line = {"t": 3.0, "kind": "ambient", "text": "Is anyone there?"}
    # A run in progress, as start() would leave it; only what _deliver_cue reads.
    engine.active = SimpleNamespace(id="x")
    engine.state = "running"

    engine._chat_cues = False  # the oracle's run
    with engine._mutex:
        engine._deliver_cue(line)
    assert engine.next_chat_input(timeout=0) is None

    engine._chat_cues = True  # the web app's run
    with engine._mutex:
        engine._deliver_cue(line)
    token, payload = engine.next_chat_input(timeout=0)
    assert payload["text"] == "Is anyone there?" and payload["sender"] == "user"
    assert engine.chat_input_is_current(token)

    heard = []
    engine.set_cue_sink(heard.append)  # a runner's run: the sink, not the outbox
    with engine._mutex:
        engine._deliver_cue(line)
    assert heard == [line]
    assert engine.next_chat_input(timeout=0) is None
