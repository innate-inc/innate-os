import math
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "workspace"))

from innate_agents.demo_agent import DemoAgent  # noqa: E402
from innate_skills import continuous_navigation as continuous  # noqa: E402
from innate_skills.continuous_navigation import (  # noqa: E402
    ContinuousNavigation,
    NavigationStatus,
    VisualDecision,
    decision_odom_goal,
    local_goal,
    parse_visual_decision,
)
from innate_skills.navigate_to_position import Nav2Controller  # noqa: E402

from innate import MainImage  # noqa: E402


def test_demo_agent_exposes_the_standalone_continuous_navigation_skill():
    assert "innate-os/continuous_navigation" in DemoAgent().skill_ids()


def test_visual_decision_contract_and_motion_compensated_goal():
    reply = """```json
    {"status":"CONTINUE","x":500,"y":800,"explanation":"clear floor ahead","progress_description":"closer"}
    ```"""
    decision = parse_visual_decision(reply, allow_keep=False)
    assert decision == VisualDecision(NavigationStatus.CONTINUE, "clear floor ahead", "closer", 500, 800)
    assert parse_visual_decision('{"status":"KEEP_CURRENT","explanation":"same route"}', allow_keep=False) is None
    assert parse_visual_decision('{"status":"CONTINUE","x":1001,"y":500,"explanation":"bad"}', allow_keep=True) is None

    capture = (1.0, 2.0, 0.3)
    odom_goal = decision_odom_goal(decision, MainImage("unused"), -20.0, capture)
    assert odom_goal is not None
    # Re-basing after the robot moves must preserve the same absolute odom
    # destination instead of replaying a stale robot-relative command.
    current = (1.2, 2.05, 0.4)
    x, y, theta = local_goal(odom_goal, current)
    c, s = math.cos(current[2]), math.sin(current[2])
    assert math.isclose(current[0] + c * x - s * y, odom_goal[0], abs_tol=1e-9)
    assert math.isclose(current[1] + s * x + c * y, odom_goal[1], abs_tol=1e-9)
    assert math.isclose(current[2] + theta, odom_goal[2], abs_tol=1e-9)


def test_visual_loop_advances_an_unfinished_goal_on_an_unchanged_route(monkeypatch):
    class FakeNavigator:
        cancelled = False

        def getPath(self, *_args, **_kwargs):
            return object()

        def goToPose(self, *_args, **_kwargs):
            return None

        def isTaskComplete(self):
            return self.cancelled

        def cancelTask(self):
            self.cancelled = True

        def getFeedback(self):
            return None

    navigator = FakeNavigator()
    controller = Nav2Controller.__new__(Nav2Controller)
    controller.skill = SimpleNamespace(sleep=lambda _seconds: None)
    controller.logger = SimpleNamespace(info=lambda _message: None, warning=lambda _message: None)
    controller.navigator = navigator
    controller.navigator_mapfree = navigator
    controller.navigator_navigation = navigator
    controller._commanded_goal_pub = SimpleNamespace(publish=lambda _goal: None)
    controller._resolve_goal = lambda x, y, theta, _local: (x, y, theta, "odom")
    controller._pose_stamped = lambda x, y, theta, frame: (x, y, theta, frame)

    # The robot has moved since its previous assessment. The same clear
    # floor pixel now represents a new absolute destination further ahead.
    frame = MainImage("unused")
    decision = VisualDecision(NavigationStatus.CONTINUE, "same clear corridor", x=500, y=800)
    first_goal = decision_odom_goal(decision, frame, -20.0, (0.0, 0.0, 0.0))
    skill = SimpleNamespace(
        _target="end of the corridor", _iteration=1, _max_iterations=50,
        _previous_image=frame, _consecutive_model_failures=0, _active_goal=first_goal,
        main_image=frame, head_position=SimpleNamespace(pitch_degrees=-20.0),
        odom=SimpleNamespace(x=1.0, y=0.0, theta=0.0), _proxy=object(),
        logger=controller.logger, check_cancelled=lambda: None, feedback=lambda _message: None,
    )
    skill._prompt = lambda **kw: ContinuousNavigation._prompt(skill, **kw)
    skill._decide = lambda **kw: ContinuousNavigation._decide(skill, **kw)

    def model(_client, _images, prompt, **kwargs):
        assert "on EVERY assessment, even when the route is unchanged" in prompt
        assert "KEEP_CURRENT" not in prompt
        assert kwargs["model"] == "gemini-3.8-flash"
        assert kwargs["reasoning_effort"] == "low"
        assert not navigator.isTaskComplete()
        return '{"status":"CONTINUE","x":500,"y":800,"explanation":"same clear corridor"}'

    monkeypatch.setattr(continuous.gemlib, "ask_image", model)
    result = controller.go_to_position(
        *first_goal, True, reassess=lambda: ContinuousNavigation._reassess_while_moving(skill)
    )

    assert result.odom_goal[0] == first_goal[0] + 1.0
    assert navigator.cancelled

    # An identical absolute destination must not cause a cancel/restart loop.
    skill._active_goal = result.odom_goal
    skill._decide = lambda **_kw: result
    assert ContinuousNavigation._reassess_while_moving(skill) is None
