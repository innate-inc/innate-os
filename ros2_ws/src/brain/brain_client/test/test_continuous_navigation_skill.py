import math
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "workspace"))

from innate_agents.demo_agent import DemoAgent  # noqa: E402
from innate_skills.continuous_navigation import (  # noqa: E402
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


def test_nav2_controller_cancels_an_inflight_goal_before_returning_replan():
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

    replacement = object()
    result = controller.go_to_position(1.0, 0.0, 0.0, True, reassess=lambda: replacement)

    assert result is replacement
    assert navigator.cancelled
