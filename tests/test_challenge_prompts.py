"""Every installed challenge can be previewed and sent to the agent."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ros2_ws/src/mars_bot/mars_sim_driver"))

from mars_sim_driver.challenges import ChallengeEngine, load_challenges  # noqa: E402


def test_all_authored_challenges_have_prompts_and_preview_metadata(tmp_path):
    roots = [ROOT / "sim/challenges", *sorted((ROOT / "sim/bundles").glob("*/challenges"))]
    challenges = load_challenges(roots)
    engine = ChallengeEngine(SimpleNamespace(), threading.Lock(), roots=[], progress_path=tmp_path / "progress.json")
    for challenge in challenges.values():
        engine.sim.environment = SimpleNamespace(id=(challenge.environments or ["apartment"])[0])
        engine.challenges = {challenge.id: challenge}
        [info] = engine.roster()
        assert info["prompt"].strip(), challenge.id
        assert info["prompt"] == (challenge.prompt.strip() or challenge.brief)
        assert info["goals"] == [g.label for g in challenge.goals]
        assert info["time_limit_s"] == challenge.time_limit_s
    assert len(challenges) >= 52
    assert challenges["victory_lap"].prompt.startswith("We won")
