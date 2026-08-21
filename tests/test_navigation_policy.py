# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Host-only contracts for navigation modes and planner routing."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MARS_NAV_ROOT = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_nav"
MODE_MANAGER = MARS_NAV_ROOT / "mars_nav" / "mode_manager.py"
ROUTER = MARS_NAV_ROOT / "mars_nav" / "navigate_to_pose_router.py"
sys.path.insert(0, str(MARS_NAV_ROOT))

from mars_nav.navigation_policy import (  # noqa: E402
    DEFAULT_MANUAL_MAX_ANGULAR_RPS,
    DEFAULT_MANUAL_MAX_LINEAR_MPS,
    DEFAULT_NAV_MAX_ANGULAR_RPS,
    DEFAULT_NAV_MAX_LINEAR_MPS,
    MAPPING_MODES,
    VALID_NAVIGATION_MODES,
    NavigationMode,
    NavigationRoute,
    configure_only_nodes,
    mapping_slow_limits,
    mapping_speed_factor,
    mapping_speed_limit_active,
    modes_nodes,
    resolve_navigation_route,
    skip_cleanup_nodes,
    starts_new_mapping_session,
)

AUTONOMOUS_MAPPING = NavigationMode.AUTONOMOUS_MAPPING.value


def test_mode_sets_match_the_lifecycle_table():
    assert VALID_NAVIGATION_MODES == frozenset({"navigation", "mapping", AUTONOMOUS_MAPPING, "mapfree"})
    assert MAPPING_MODES == frozenset({"mapping", AUTONOMOUS_MAPPING})
    assert set(modes_nodes) == VALID_NAVIGATION_MODES


@pytest.mark.parametrize("target", [NavigationMode.MAPPING.value, AUTONOMOUS_MAPPING])
def test_mapping_session_starts_only_when_slam_frame_starts(target):
    assert starts_new_mapping_session(NavigationMode.NAV.value, target)
    assert starts_new_mapping_session(target, target, first_start=True)
    assert not starts_new_mapping_session(NavigationMode.MAPPING.value, target)
    assert not starts_new_mapping_session(AUTONOMOUS_MAPPING, target)


def test_nonmapping_modes_never_start_mapping_sessions():
    assert not starts_new_mapping_session(NavigationMode.MAPPING.value, NavigationMode.NAV.value)
    assert not starts_new_mapping_session(None, NavigationMode.MAPFREE.value, first_start=True)


def test_mapping_failure_rollback_requires_a_live_nonstartup_handoff():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    assert "not first_start and previous_mode in MAPPING_MODES and (target_mode in MAPPING_MODES)" in ast.unparse(
        method
    )


def test_autonomous_mapping_has_exact_lifecycle_contract():
    assert modes_nodes[AUTONOMOUS_MAPPING] == [
        "slam_toolbox",
        "mapfree/planner_server",
        "navigation/planner_server",
        "controller_server",
        "behavior_server",
        "velocity_smoother",
        "bt_navigator",
    ]
    assert configure_only_nodes == {
        AUTONOMOUS_MAPPING: {"mapfree/planner_server"},
        NavigationMode.MAPFREE.value: {"navigation/planner_server"},
    }
    assert skip_cleanup_nodes == {
        "controller_server",
        "mapfree/planner_server",
        "navigation/planner_server",
    }


def test_autonomous_mapping_activates_only_live_map_navigation_nodes():
    configured_only = configure_only_nodes[AUTONOMOUS_MAPPING]
    active = [node for node in modes_nodes[AUTONOMOUS_MAPPING] if node not in configured_only]
    assert active == [
        "slam_toolbox",
        "navigation/planner_server",
        "controller_server",
        "behavior_server",
        "velocity_smoother",
        "bt_navigator",
    ]
    assert {
        "navigation_map_server",
        "navigation_amcl",
        "navigation_grid_localizer",
        "null_map_node",
    }.isdisjoint(modes_nodes[AUTONOMOUS_MAPPING])


def test_autonomous_mapping_orders_every_bt_dependency_before_navigator():
    nodes = modes_nodes[AUTONOMOUS_MAPPING]
    assert nodes[0] == "slam_toolbox"
    bt_index = nodes.index("bt_navigator")
    for dependency in (
        "mapfree/planner_server",
        "navigation/planner_server",
        "controller_server",
        "behavior_server",
        "velocity_smoother",
    ):
        assert nodes.index(dependency) < bt_index


def test_manual_mapping_remains_slam_only():
    assert modes_nodes[NavigationMode.MAPPING.value] == ["slam_toolbox"]
    assert NavigationMode.MAPPING.value not in configure_only_nodes


@pytest.mark.parametrize(
    "mode",
    [None, "", "none", "unknown", "switching", "mapping", AUTONOMOUS_MAPPING],
)
def test_mapping_speed_limit_fails_slow_until_a_stable_non_mapping_mode(mode):
    assert mapping_speed_limit_active(mode)


@pytest.mark.parametrize("mode", ["navigation", "mapfree"])
def test_mapping_speed_limit_releases_only_in_a_stable_non_mapping_mode(mode):
    assert not mapping_speed_limit_active(mode)


def test_mapping_speed_envelope_matches_the_canonical_slow_preset():
    linear_cap, angular_cap = mapping_slow_limits(
        DEFAULT_MANUAL_MAX_LINEAR_MPS,
        DEFAULT_MANUAL_MAX_ANGULAR_RPS,
        DEFAULT_NAV_MAX_LINEAR_MPS,
        DEFAULT_NAV_MAX_ANGULAR_RPS,
    )

    assert linear_cap == pytest.approx(0.14)
    assert angular_cap == pytest.approx(0.21)
    assert mapping_speed_factor(True, 0.4, 0.0, linear_cap, angular_cap) == pytest.approx(0.35)
    assert mapping_speed_factor(True, -0.4, 0.0, linear_cap, angular_cap) == pytest.approx(0.35)
    assert mapping_speed_factor(True, 0.0, 1.0, linear_cap, angular_cap) == pytest.approx(0.21)


def test_mapping_speed_envelope_uses_the_lower_manual_and_nav_cap_before_slow_scale():
    assert mapping_slow_limits(0.2, 1.5, 0.45, 0.4) == pytest.approx((0.07, 0.14))
    assert mapping_slow_limits(0.8, 0.3, 0.1, 0.6) == pytest.approx((0.035, 0.105))


def test_mapping_speed_envelope_preserves_curvature_and_does_not_double_limit_slow_input():
    linear_cap, angular_cap = mapping_slow_limits(
        DEFAULT_MANUAL_MAX_LINEAR_MPS,
        DEFAULT_MANUAL_MAX_ANGULAR_RPS,
        DEFAULT_NAV_MAX_LINEAR_MPS,
        DEFAULT_NAV_MAX_ANGULAR_RPS,
    )

    factor = mapping_speed_factor(True, 0.4, 0.8, linear_cap, angular_cap)
    assert factor == pytest.approx(0.2625)
    assert (0.4 * factor) / (0.8 * factor) == pytest.approx(0.4 / 0.8)
    assert mapping_speed_factor(True, 0.1, 0.2, linear_cap, angular_cap) == 1.0
    assert mapping_speed_factor(False, 4.0, 8.0, linear_cap, angular_cap) == 1.0


@pytest.mark.parametrize(
    ("linear", "angular", "linear_cap", "angular_cap"),
    [
        (float("nan"), 0.0, 0.14, 0.21),
        (0.0, float("inf"), 0.14, 0.21),
        (0.1, 0.1, 0.0, 0.21),
        (0.1, 0.1, 0.14, -1.0),
    ],
)
def test_mapping_speed_envelope_fails_to_zero_for_invalid_commands_or_caps(linear, angular, linear_cap, angular_cap):
    assert mapping_speed_factor(True, linear, angular, linear_cap, angular_cap) == 0.0


@pytest.mark.parametrize("invalid", [0.0, -1.0, float("nan"), float("inf")])
def test_mapping_speed_limits_fail_closed_for_invalid_config(invalid):
    assert mapping_slow_limits(invalid, 1.0, 0.45, 0.6) == (0.0, 0.0)


def test_every_configure_only_node_belongs_to_its_mode():
    for mode, configured_only in configure_only_nodes.items():
        assert configured_only <= set(modes_nodes[mode])


NAVIGATION_ROUTE = NavigationRoute(planner="navigation", goal_checker="goal_checker")
MAPFREE_ROUTE = NavigationRoute(planner="mapfree", goal_checker="goal_checker_precise")


@pytest.mark.parametrize(
    ("mode", "selector", "expected"),
    [
        ("navigation", None, NAVIGATION_ROUTE),
        ("navigation", "", NAVIGATION_ROUTE),
        ("navigation", "navigation", NAVIGATION_ROUTE),
        ("navigation", "mapfree", MAPFREE_ROUTE),
        ("mapfree", None, MAPFREE_ROUTE),
        ("mapfree", "mapfree", MAPFREE_ROUTE),
        (AUTONOMOUS_MAPPING, None, NAVIGATION_ROUTE),
        (AUTONOMOUS_MAPPING, "navigation", NAVIGATION_ROUTE),
        (AUTONOMOUS_MAPPING, AUTONOMOUS_MAPPING, NAVIGATION_ROUTE),
        ("  AUTONOMOUS_MAPPING  ", "  NAVIGATION ", NAVIGATION_ROUTE),
    ],
)
def test_navigation_route_matrix(mode, selector, expected):
    assert resolve_navigation_route(mode, selector) == expected


@pytest.mark.parametrize(
    ("mode", "selector"),
    [
        (None, None),
        ("", None),
        ("none", None),
        ("switching", "navigation"),
        ("unknown", None),
        ("mapping", None),
        ("mapping", "navigation"),
        (AUTONOMOUS_MAPPING, "mapfree"),
        ("mapfree", "navigation"),
        ("navigation", "mapping"),
        ("navigation", AUTONOMOUS_MAPPING),
        ("navigation", "/tmp/custom_tree.xml"),
    ],
)
def test_navigation_routes_fail_closed(mode, selector):
    assert resolve_navigation_route(mode, selector) is None


def _is_mode_lock_call(node: ast.AST, method_name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
        and ast.unparse(node.func.value) == "self._mode_change_lock"
    )


def test_save_map_serializes_with_mode_and_map_operations():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "save_map_callback"
    )

    acquire = next(node for node in ast.walk(method) if _is_mode_lock_call(node, "acquire"))
    assert any(
        keyword.arg == "blocking" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for keyword in acquire.keywords
    )

    guarded_try = next(node for node in method.body if isinstance(node, ast.Try) and node.finalbody)
    assert acquire.lineno < guarded_try.lineno
    assert any(
        _is_mode_lock_call(node, "release") for statement in guarded_try.finalbody for node in ast.walk(statement)
    )


def test_save_map_quiesces_before_lock_and_revalidates_before_file_mutation():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "save_map_callback"
    )
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]

    quiesce = next(
        node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "change_mode_callback"
    )
    acquire = next(node for node in calls if _is_mode_lock_call(node, "acquire"))
    mode_revalidation = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "self.current_mode != NavigationMode.MAPPING.value"
    )
    first_map_mutation = min(
        node.lineno
        for node in calls
        if (isinstance(node.func, ast.Attribute) and node.func.attr in {"makedirs", "remove"})
        or (isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == "subprocess.run")
    )
    assert quiesce.lineno < acquire.lineno < mode_revalidation.lineno < first_map_mutation

    autonomous_guard = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "self.current_mode == NavigationMode.AUTONOMOUS_MAPPING.value"
    )
    assert any(descendant is quiesce for descendant in ast.walk(autonomous_guard))
    assert any(
        isinstance(node, ast.If) and ast.unparse(node.test) == "not mode_response.success"
        for node in ast.walk(autonomous_guard)
    )


def test_save_map_racing_autonomous_restart_fails_before_file_mutation():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "save_map_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)

    events = []

    class ChangeMode:
        class Request:
            mode = ""

        class Response:
            success = False
            message = ""

    class RacingLock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("save_lock")
            manager.current_mode = AUTONOMOUS_MAPPING
            return True

        def release(self):
            events.append("release")

    class Logger:
        def error(self, _message):
            pass

        def warning(self, _message):
            pass

    def fail_file_mutation(*_args, **_kwargs):
        events.append("file_mutation")
        raise AssertionError("map files must not be touched after autonomous mapping restarts")

    namespace = {
        "ChangeNavigationMode": ChangeMode,
        "NavigationMode": NavigationMode,
        "MAPPING_MODES": MAPPING_MODES,
        "os": types.SimpleNamespace(
            makedirs=fail_file_mutation,
            remove=fail_file_mutation,
            path=types.SimpleNamespace(join=lambda *_parts: "unused", exists=lambda _path: False),
        ),
        "subprocess": types.SimpleNamespace(TimeoutExpired=TimeoutError, run=fail_file_mutation),
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    manager = types.SimpleNamespace(
        current_mode=AUTONOMOUS_MAPPING,
        _mode_change_lock=RacingLock(),
        get_logger=lambda: Logger(),
    )

    def quiesce(request, mode_response, first_start=False):
        assert request.mode == NavigationMode.MAPPING.value
        assert first_start is False
        events.append("quiesce")
        manager.current_mode = NavigationMode.MAPPING.value
        mode_response.success = True
        mode_response.message = "manual mapping active"
        return mode_response

    manager.change_mode_callback = quiesce
    request = types.SimpleNamespace(map_name="safe_name", overwrite=False)
    response = types.SimpleNamespace(success=None, message="")

    returned = namespace["save_map_callback"](manager, request, response)

    assert returned is response
    assert response.success is False
    assert "manual mapping is not active" in response.message
    assert events == ["quiesce", "save_lock", "release"]


def test_router_fails_closed_before_waiting_for_internal_action_server():
    tree = ast.parse(ROUTER.read_text())
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_goal_callback", "_execute_reserved_goal"}
    }

    goal_callback_source = ast.unparse(methods["_goal_callback"])
    assert "resolve_navigation_route" in goal_callback_source
    assert "GoalResponse.REJECT" in goal_callback_source

    execute_calls = [node for node in ast.walk(methods["_execute_reserved_goal"]) if isinstance(node, ast.Call)]
    first_resolve = min(
        node.lineno
        for node in execute_calls
        if isinstance(node.func, ast.Name) and node.func.id == "resolve_navigation_route"
    )
    wait_for_server = min(
        node.lineno
        for node in execute_calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "wait_for_server"
    )
    assert first_resolve < wait_for_server


def test_mode_switch_closes_admission_before_cancel_and_restores_on_failure():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    source = ast.unparse(method)

    capture_previous = source.index("previous_mode = self.current_mode")
    arm_slow = source.index("self._set_mapping_speed_limit(True)", capture_previous)
    publish_switching = source.index("self.current_mode = 'switching'")
    cancel_navigation = source.index("self._cancel_active_navigation()")
    assert capture_previous < arm_slow < publish_switching < cancel_navigation

    cancellation_failure = next(
        node for node in ast.walk(method) if isinstance(node, ast.If) and ast.unparse(node.test) == "not cancelled_ok"
    )
    failure_source = ast.unparse(cancellation_failure)
    assert "self.current_mode = previous_mode" in failure_source
    assert "self.publish_status()" in failure_source
    restored_publish = failure_source.index("self.publish_status()")
    release_slow = failure_source.index("self._release_mapping_speed_limit_after_stable_mode()")
    assert restored_publish < release_slow


def test_mode_switch_requires_slow_ack_before_admission_cancel_or_lifecycle_mutation():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "MAPPING_MODES": MAPPING_MODES,
        "NavigationMode": NavigationMode,
        "VALID_NAVIGATION_MODES": VALID_NAVIGATION_MODES,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

        def debug(self, _message):
            pass

    manager = types.SimpleNamespace(
        available_maps=["home.yaml"],
        current_map="home.yaml",
        current_mode=NavigationMode.NAV.value,
        _mode_change_lock=Lock(),
        _switch_generation=0,
        get_logger=lambda: Logger(),
    )
    manager._set_mapping_speed_limit = lambda enabled: (
        events.append(f"limit:{enabled}") or (False, "no acknowledgement")
    )
    manager.publish_status = lambda: (_ for _ in ()).throw(AssertionError("must not publish switching"))
    manager._cancel_active_navigation = lambda: (_ for _ in ()).throw(AssertionError("must not cancel"))
    manager.request_mode_startup = lambda _mode: (_ for _ in ()).throw(AssertionError("must not mutate lifecycle"))

    response = types.SimpleNamespace(success=None, message="")
    returned = namespace["change_mode_callback"](
        manager,
        types.SimpleNamespace(mode=NavigationMode.MAPPING.value),
        response,
    )

    assert returned is response
    assert response.success is False
    assert "before transition" in response.message
    assert manager.current_mode == NavigationMode.NAV.value
    assert events == ["lock", "limit:True", "error", "release"]


def test_first_start_slow_ack_failure_publishes_none_then_saved_mode_request_starts_lifecycle():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "MAPPING_MODES": MAPPING_MODES,
        "NavigationMode": NavigationMode,
        "VALID_NAVIGATION_MODES": VALID_NAVIGATION_MODES,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []
    saved_modes = []
    enable_attempts = 0

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

        def debug(self, _message):
            pass

    manager = types.SimpleNamespace(
        available_maps=["home.yaml"],
        current_map="home.yaml",
        current_mode=NavigationMode.MAPFREE.value,
        _mode_change_lock=Lock(),
        _switch_generation=0,
        get_logger=lambda: Logger(),
    )

    def set_slow(enabled):
        nonlocal enable_attempts
        events.append(f"limit:{enabled}")
        if enabled:
            enable_attempts += 1
            if enable_attempts == 1:
                return False, "no acknowledgement"
        return True, "ok"

    manager._set_mapping_speed_limit = set_slow
    manager.publish_status = lambda: events.append(f"publish:{manager.current_mode}")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (True, "stopped")
    manager.request_mode_startup = lambda mode: events.append(f"lifecycle:{mode.value}") or (True, "started")
    manager.save_last_mode = lambda mode: (saved_modes.append(mode), events.append(f"persist:{mode}"))
    manager._release_mapping_speed_limit_after_stable_mode = lambda: (
        events.append(f"release_slow:{manager.current_mode}") or (True, "released")
    )
    request = types.SimpleNamespace(mode=NavigationMode.MAPFREE.value)

    first_response = namespace["change_mode_callback"](
        manager,
        request,
        types.SimpleNamespace(success=None, message=""),
        first_start=True,
    )

    assert first_response.success is False
    assert manager.current_mode == "none"
    assert saved_modes == []
    assert "lifecycle:mapfree" not in events
    assert events == ["lock", "limit:True", "publish:none", "error", "lock:release"]

    second_response = namespace["change_mode_callback"](
        manager,
        request,
        types.SimpleNamespace(success=None, message=""),
    )

    assert second_response.success is True
    assert manager.current_mode == NavigationMode.MAPFREE.value
    assert saved_modes == [NavigationMode.MAPFREE.value]
    assert events[5:] == [
        "lock",
        "limit:True",
        "publish:switching",
        "cancel",
        "lifecycle:mapfree",
        "persist:mapfree",
        "publish:mapfree",
        "release_slow:mapfree",
        "lock:release",
    ]


@pytest.mark.parametrize(
    ("previous_mode", "target_mode"),
    [
        (NavigationMode.MAPPING, NavigationMode.AUTONOMOUS_MAPPING),
        (NavigationMode.AUTONOMOUS_MAPPING, NavigationMode.MAPPING),
    ],
)
def test_failed_mapping_handoff_restores_previous_stack_and_retry_keeps_session(previous_mode, target_mode):
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "MAPPING_MODES": MAPPING_MODES,
        "NavigationMode": NavigationMode,
        "VALID_NAVIGATION_MODES": VALID_NAVIGATION_MODES,
        "starts_new_mapping_session": starts_new_mapping_session,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []
    target_attempts = 0

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

        def debug(self, _message):
            pass

    manager = types.SimpleNamespace(
        available_maps=["home.yaml"],
        current_map="home.yaml",
        current_mode=previous_mode.value,
        _mapping_session_started=1234.5,
        _mode_change_lock=Lock(),
        _switch_generation=0,
        get_logger=lambda: Logger(),
    )
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager.publish_status = lambda: events.append(f"publish:{manager.current_mode}")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (True, "stopped")

    def start(mode):
        nonlocal target_attempts
        events.append(f"lifecycle:{mode.value}")
        if mode == target_mode:
            target_attempts += 1
            if target_attempts == 1:
                return False, "controller failed"
        return True, "started"

    manager.request_mode_startup = start
    manager.save_last_mode = lambda mode: events.append(f"persist:{mode}")
    manager.mapping_session_publisher = types.SimpleNamespace(
        publish=lambda _msg: (_ for _ in ()).throw(AssertionError("mapping handoff minted a new session"))
    )
    request = types.SimpleNamespace(mode=target_mode.value)

    failed = namespace["change_mode_callback"](
        manager,
        request,
        types.SimpleNamespace(success=None, message=""),
    )
    assert failed.success is False
    assert f"restored {previous_mode.value} mode" in failed.message
    assert manager.current_mode == previous_mode.value
    assert manager._mapping_session_started == 1234.5

    retried = namespace["change_mode_callback"](
        manager,
        request,
        types.SimpleNamespace(success=None, message=""),
    )
    assert retried.success is True
    assert manager.current_mode == target_mode.value
    assert manager._mapping_session_started == 1234.5
    assert events.count(f"lifecycle:{previous_mode.value}") == 1
    assert events.count(f"lifecycle:{target_mode.value}") == 2


def test_mode_cancel_failure_restores_stable_status_then_attempts_slow_release():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "MAPPING_MODES": MAPPING_MODES,
        "NavigationMode": NavigationMode,
        "VALID_NAVIGATION_MODES": VALID_NAVIGATION_MODES,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

        def debug(self, _message):
            pass

    manager = types.SimpleNamespace(
        available_maps=["home.yaml"],
        current_map="home.yaml",
        current_mode=NavigationMode.NAV.value,
        _mode_change_lock=Lock(),
        _switch_generation=0,
        get_logger=lambda: Logger(),
    )
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager.publish_status = lambda: events.append(f"publish:{manager.current_mode}")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (False, "goal still active")
    manager._release_mapping_speed_limit_after_stable_mode = lambda: (
        events.append(f"release_slow:{manager.current_mode}") or (False, "release unavailable")
    )
    manager.request_mode_startup = lambda _mode: (_ for _ in ()).throw(AssertionError("must not mutate lifecycle"))

    response = types.SimpleNamespace(success=None, message="")
    returned = namespace["change_mode_callback"](
        manager,
        types.SimpleNamespace(mode=NavigationMode.MAPPING.value),
        response,
    )

    assert returned is response
    assert response.success is False
    assert "could not stop active navigation (goal still active)" in response.message
    assert "Slow drive envelope remains active (release unavailable)" in response.message
    assert manager.current_mode == NavigationMode.NAV.value
    assert events == [
        "lock",
        "limit:True",
        "publish:switching",
        "cancel",
        "publish:navigation",
        "release_slow:navigation",
        "error",
        "lock:release",
    ]


def test_successful_nonmapping_mode_releases_slow_only_after_stable_status_publish():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_mode_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "MAPPING_MODES": MAPPING_MODES,
        "NavigationMode": NavigationMode,
        "VALID_NAVIGATION_MODES": VALID_NAVIGATION_MODES,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            pass

        def debug(self, _message):
            events.append("return")

    manager = types.SimpleNamespace(
        available_maps=["home.yaml"],
        current_map="home.yaml",
        current_mode=NavigationMode.MAPPING.value,
        _mode_change_lock=Lock(),
        _switch_generation=0,
        get_logger=lambda: Logger(),
    )
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager.publish_status = lambda: events.append(f"publish:{manager.current_mode}")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (True, "stopped")
    manager.request_mode_startup = lambda mode: events.append(f"lifecycle:{mode.value}") or (True, "started")
    manager.save_last_mode = lambda mode: events.append(f"persist:{mode}")

    def release_after_stable():
        events.append(f"release_slow:{manager.current_mode}")
        assert manager.current_mode == NavigationMode.MAPFREE.value
        return True, "released"

    manager._release_mapping_speed_limit_after_stable_mode = release_after_stable
    response = types.SimpleNamespace(success=None, message="")
    returned = namespace["change_mode_callback"](
        manager,
        types.SimpleNamespace(mode=NavigationMode.MAPFREE.value),
        response,
    )

    assert returned is response
    assert response.success is True
    assert events == [
        "lock",
        "limit:True",
        "publish:switching",
        "cancel",
        "lifecycle:mapfree",
        "persist:mapfree",
        "publish:mapfree",
        "release_slow:mapfree",
        "lock:release",
    ]


def test_map_switch_closes_admission_and_cancels_before_map_or_lifecycle_mutation():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_map_callback"
    )
    source = ast.unparse(method)

    capture_previous = source.index("previous_mode = self.current_mode")
    arm_slow = source.index("self._set_mapping_speed_limit(True)", capture_previous)
    publish_switching = source.index("self.current_mode = 'switching'")
    cancel_navigation = source.index("self._cancel_active_navigation()")
    set_requested_map = source.index("self.current_map = requested_map")
    switch_lifecycle = source.index("self._efficient_map_switch()")
    assert capture_previous < arm_slow < publish_switching < cancel_navigation < set_requested_map < switch_lifecycle


def test_map_switch_cancel_failure_restores_mode_without_mutating_map_or_lifecycle():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_map_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "NavigationMode": NavigationMode,
        "time": types.SimpleNamespace(monotonic=lambda: 123.0),
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

    manager = types.SimpleNamespace(
        available_maps=["old.yaml", "next.yaml"],
        current_map="old.yaml",
        current_mode="navigation",
        _mode_change_lock=Lock(),
        _switch_generation=7,
        get_logger=lambda: Logger(),
    )

    def publish_status():
        events.append(f"publish:{manager.current_mode}:{manager.current_map}")

    def cancel_active_navigation():
        events.append("cancel")
        return False, "goal still active"

    def fail_lifecycle():
        events.append("lifecycle")
        raise AssertionError("lifecycle must remain untouched after cancellation failure")

    manager.publish_status = publish_status
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager._release_mapping_speed_limit_after_stable_mode = lambda: (
        events.append("limit:release")
        or (
            False,
            "release unavailable",
        )
    )
    manager._cancel_active_navigation = cancel_active_navigation
    manager._efficient_map_switch = fail_lifecycle
    manager.save_last_map = lambda _name: events.append("persist")
    manager._trigger_relocalization = lambda **_kwargs: events.append("relocalize")
    request = types.SimpleNamespace(map_name="next.yaml")
    response = types.SimpleNamespace(success=None, message="")

    returned = namespace["change_map_callback"](manager, request, response)

    assert returned is response
    assert response.success is False
    assert "could not stop active navigation" in response.message
    assert "Slow drive envelope remains active (release unavailable)" in response.message
    assert manager.current_mode == "navigation"
    assert manager.current_map == "old.yaml"
    assert manager._switch_generation == 8
    assert events == [
        "lock",
        "limit:True",
        "publish:switching:old.yaml",
        "cancel",
        "publish:navigation:old.yaml",
        "error",
        "limit:release",
        "release",
    ]


def test_map_cancel_retries_restored_status_before_releasing_slow_if_first_publish_throws():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_map_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "NavigationMode": NavigationMode,
        "time": types.SimpleNamespace(monotonic=lambda: 123.0),
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []
    stable_publish_attempts = 0

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

    manager = types.SimpleNamespace(
        available_maps=["old.yaml", "next.yaml"],
        current_map="old.yaml",
        current_mode=NavigationMode.NAV.value,
        _mode_change_lock=Lock(),
        _switch_generation=7,
        get_logger=lambda: Logger(),
    )

    def publish_status():
        nonlocal stable_publish_attempts
        events.append(f"publish:{manager.current_mode}:{manager.current_map}")
        if manager.current_mode == NavigationMode.NAV.value:
            stable_publish_attempts += 1
            if stable_publish_attempts == 1:
                raise RuntimeError("transient stable-status publish failure")

    manager.publish_status = publish_status
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (False, "goal still active")
    manager._release_mapping_speed_limit_after_stable_mode = lambda: (
        events.append("limit:release")
        or (
            True,
            "released",
        )
    )
    manager._efficient_map_switch = lambda: (_ for _ in ()).throw(AssertionError("must not mutate lifecycle"))
    manager.save_last_map = lambda _name: events.append("persist")
    manager._trigger_relocalization = lambda **_kwargs: events.append("relocalize")
    response = types.SimpleNamespace(success=None, message="")

    returned = namespace["change_map_callback"](
        manager,
        types.SimpleNamespace(map_name="next.yaml"),
        response,
    )

    assert returned is response
    assert response.success is False
    assert "could not stop active navigation (goal still active)" in response.message
    assert "additional error while changing map: transient stable-status publish failure" in response.message
    assert stable_publish_attempts == 2
    restored_publishes = [index for index, event in enumerate(events) if event == "publish:navigation:old.yaml"]
    assert len(restored_publishes) == 2
    assert restored_publishes[0] < restored_publishes[1] < events.index("limit:release")
    assert events.index("limit:release") < events.index("lock:release")
    assert "persist" not in events
    assert "relocalize" not in events


@pytest.mark.parametrize("failure_kind", ["lifecycle_result", "exception"])
def test_map_switch_failures_restore_status_then_attempt_slow_release_without_masking_failure(failure_kind):
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "change_map_callback"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)
    namespace = {
        "NavigationMode": NavigationMode,
        "time": types.SimpleNamespace(monotonic=lambda: 123.0),
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("lock:release")

    class Logger:
        def info(self, _message):
            pass

        def warning(self, _message):
            pass

        def error(self, _message):
            events.append("error")

    manager = types.SimpleNamespace(
        available_maps=["old.yaml", "next.yaml"],
        current_map="old.yaml",
        current_mode=NavigationMode.NAV.value,
        _mode_change_lock=Lock(),
        _switch_generation=7,
        get_logger=lambda: Logger(),
    )
    manager.publish_status = lambda: events.append(f"publish:{manager.current_mode}:{manager.current_map}")
    manager._set_mapping_speed_limit = lambda enabled: events.append(f"limit:{enabled}") or (True, "ok")
    manager._cancel_active_navigation = lambda: events.append("cancel") or (True, "stopped")
    manager._release_mapping_speed_limit_after_stable_mode = lambda: (
        events.append("limit:release")
        or (
            False,
            "release unavailable",
        )
    )

    def fail_map_switch():
        events.append("lifecycle")
        if failure_kind == "exception":
            raise RuntimeError("map lifecycle exploded")
        return False, "lifecycle incomplete"

    manager._efficient_map_switch = fail_map_switch
    manager.save_last_map = lambda _name: events.append("persist")
    manager._trigger_relocalization = lambda **_kwargs: events.append("relocalize")
    response = types.SimpleNamespace(success=None, message="")

    returned = namespace["change_map_callback"](
        manager,
        types.SimpleNamespace(map_name="next.yaml"),
        response,
    )

    assert returned is response
    assert response.success is False
    if failure_kind == "exception":
        assert "Error changing map: map lifecycle exploded" in response.message
    else:
        assert "Failed to switch to map 'next.yaml': lifecycle incomplete" in response.message
    assert "Slow drive envelope remains active (release unavailable)" in response.message
    assert manager.current_mode == NavigationMode.NAV.value
    assert manager.current_map == "old.yaml"
    restored_status = events.index("publish:navigation:old.yaml", events.index("publish:switching:old.yaml") + 1)
    release_slow = events.index("limit:release")
    lock_release = events.index("lock:release")
    assert restored_status < release_slow < lock_release
    assert "persist" not in events
    assert "relocalize" not in events


def test_mapping_pose_uses_the_tf_sample_stamp_not_wheel_odometry_time():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "odom_callback")
    source = ast.unparse(method)

    assert "odom_msg.header.stamp = tf.header.stamp" in source
    assert "odom_msg.header.stamp = msg.header.stamp" not in source


def test_watchdog_discards_old_stack_repairs_if_mode_changes_during_unlocked_probe():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_lifecycle_watchdog"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)

    class State:
        PRIMARY_STATE_UNCONFIGURED = 1
        PRIMARY_STATE_INACTIVE = 2
        PRIMARY_STATE_ACTIVE = 3

    events = []

    class Lock:
        def acquire(self, *, blocking):
            assert blocking is False
            events.append("lock")
            return True

        def release(self):
            events.append("release")

    class Logger:
        def info(self, message):
            events.append(f"info:{message}")

        def warning(self, _message):
            pass

        def error(self, _message):
            pass

    manager = types.SimpleNamespace(
        _lifecycle_watchdog_armed=True,
        _watchdog_busy=False,
        current_mode=NavigationMode.MAPPING.value,
        _mode_change_lock=Lock(),
        _service_clients={},
        get_logger=lambda: Logger(),
    )

    def get_state(_clients, _logger, node_name, timeout_sec):
        assert node_name == "slam_toolbox"
        assert timeout_sec == 2.0
        events.append("probe:mapping")
        # Simulate a complete mapping -> navigation transition while the
        # watchdog is outside the lifecycle lock.
        manager.current_mode = NavigationMode.NAV.value
        return State.PRIMARY_STATE_UNCONFIGURED

    def fail_transition(*_args, **_kwargs):
        events.append("restore")
        raise AssertionError("watchdog must not restore nodes from the old mode")

    namespace = {
        "State": State,
        "configure_only_nodes": configure_only_nodes,
        "get_node_state": get_state,
        "modes_nodes": modes_nodes,
        "transition_node": fail_transition,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    namespace["_lifecycle_watchdog"](manager)

    assert manager.current_mode == NavigationMode.NAV.value
    assert manager._watchdog_busy is False
    assert events[0:2] == ["probe:mapping", "lock"]
    assert "restore" not in events
    assert events[-1] == "release"


def test_mode_status_synchronously_updates_router_before_dds_publish():
    mode_tree = ast.parse(MODE_MANAGER.read_text())
    mode_methods = {
        node.name: node for node in ast.walk(mode_tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    publish_source = ast.unparse(mode_methods["publish_status"])
    sink_update = publish_source.index("self._navigation_mode_sink(self.current_mode)")
    dds_publish = publish_source.index("self.mode_publisher.publish(mode_msg)")
    assert sink_update < dds_publish

    main_source = ast.unparse(mode_methods["main"])
    sink_binding = main_source.index("mode_manager.set_navigation_mode_sink(navigate_to_pose_router.set_current_mode)")
    pending_binding = main_source.index(
        "mode_manager.set_navigation_pending_goals_getter(navigate_to_pose_router.get_pending_goal_count)"
    )
    executor_spin = main_source.index("executor.spin()")
    assert sink_binding < pending_binding < executor_spin

    router_tree = ast.parse(ROUTER.read_text())
    set_mode = next(
        node for node in ast.walk(router_tree) if isinstance(node, ast.FunctionDef) and node.name == "set_current_mode"
    )
    set_mode_source = ast.unparse(set_mode)
    assert "self._current_mode = next_mode" in set_mode_source
    assert "with self._admission_lock" in set_mode_source


def test_mode_teardown_waits_for_internal_and_router_goals_with_a_deadline():
    mode_tree = ast.parse(MODE_MANAGER.read_text())
    cancel_method = next(
        node
        for node in ast.walk(mode_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_cancel_active_navigation"
    )
    source = ast.unparse(cancel_method)

    assert "pending_goals = self._navigation_pending_goals()" in source
    assert "self._nav_active_goals == 0 and pending_goals == 0" in source
    cancel_rpc = source.index("response = call_service")
    terminal_deadline = source.index("terminal_deadline = time.monotonic() + terminal_timeout_sec")
    assert cancel_rpc < terminal_deadline
    assert "CancelGoal.Request(), rpc_timeout_sec" in source
    assert "while time.monotonic() < terminal_deadline" in source
    assert "self._nav_active_goals > 0 or pending_goals > 0" in source
    assert "router={pending_goals}" in source


def test_router_admission_reservation_is_locked_and_always_released():
    tree = ast.parse(ROUTER.read_text())
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    goal_source = ast.unparse(methods["_goal_callback"])
    execute_source = ast.unparse(methods["_execute_callback"])

    assert "with self._admission_lock" in goal_source
    assert "self._pending_goal_reservations += 1" in goal_source
    assert "finally:\n        self._release_goal_reservation()" in execute_source


def test_router_cancels_an_internal_goal_if_mode_changes_during_handoff():
    tree = ast.parse(ROUTER.read_text())
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}
    execute_source = ast.unparse(methods["_execute_reserved_goal"])
    cancel_source = ast.unparse(methods["_cancel_internal_and_wait_for_terminal"])

    internal_acceptance = execute_source.index("client_goal_handle = await send_goal_future")
    handoff_mode_check = execute_source.index("self._current_mode != admitted_mode")
    handoff_cancel = execute_source.index("await self._cancel_internal_and_wait_for_terminal(client_goal_handle)")
    terminal_check = execute_source.index("internal_canceled =", handoff_cancel)
    outer_abort = execute_source.index("goal_handle.abort()", handoff_mode_check)
    assert internal_acceptance < handoff_mode_check < handoff_cancel < terminal_check < outer_abort

    result_registration = cancel_source.index("result_future = client_goal_handle.get_result_async()")
    cancel_ack = cancel_source.index("await client_goal_handle.cancel_goal_async()")
    terminal_result = cancel_source.index("result_response = await result_future")
    assert result_registration < cancel_ack < terminal_result


def _load_router_for_host_test(monkeypatch):
    """Import the router with tiny ROS stubs so its async safety helper is testable."""

    class Dummy:
        pass

    class DummyNavigateToPose:
        class Result:
            pass

    nav2_msgs = types.ModuleType("nav2_msgs")
    nav2_msgs_action = types.ModuleType("nav2_msgs.action")
    nav2_msgs_action.NavigateToPose = DummyNavigateToPose
    nav2_msgs.action = nav2_msgs_action

    rclpy = types.ModuleType("rclpy")
    rclpy_action = types.ModuleType("rclpy.action")
    rclpy_action.ActionClient = Dummy
    rclpy_action.ActionServer = Dummy
    rclpy_action.CancelResponse = Dummy

    class DummyGoalResponse:
        ACCEPT = object()
        REJECT = object()

    rclpy_action.GoalResponse = DummyGoalResponse
    rclpy_callback_groups = types.ModuleType("rclpy.callback_groups")
    rclpy_callback_groups.MutuallyExclusiveCallbackGroup = Dummy
    rclpy_node = types.ModuleType("rclpy.node")
    rclpy_node.Node = Dummy
    rclpy_qos = types.ModuleType("rclpy.qos")
    rclpy_qos.QoSDurabilityPolicy = Dummy
    rclpy_qos.QoSProfile = Dummy
    rclpy_qos.QoSReliabilityPolicy = Dummy

    std_msgs = types.ModuleType("std_msgs")
    std_msgs_msg = types.ModuleType("std_msgs.msg")
    std_msgs_msg.String = Dummy
    std_msgs.msg = std_msgs_msg

    for name, module in {
        "nav2_msgs": nav2_msgs,
        "nav2_msgs.action": nav2_msgs_action,
        "rclpy": rclpy,
        "rclpy.action": rclpy_action,
        "rclpy.callback_groups": rclpy_callback_groups,
        "rclpy.node": rclpy_node,
        "rclpy.qos": rclpy_qos,
        "std_msgs": std_msgs,
        "std_msgs.msg": std_msgs_msg,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("navigate_to_pose_router_host_test", ROUTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_in_process_mode_update_atomically_closes_admission_and_preserves_reservation(monkeypatch):
    module = _load_router_for_host_test(monkeypatch)
    logs = []

    class Logger:
        def info(self, message):
            logs.append(("info", message))

        def debug(self, message):
            logs.append(("debug", message))

        def warn(self, message):
            logs.append(("warn", message))

        def error(self, message):
            logs.append(("error", message))

    router = types.SimpleNamespace(
        _admission_lock=threading.Lock(),
        _current_mode="navigation",
        _has_authoritative_mode_source=False,
        _pending_goal_reservations=0,
        get_logger=lambda: Logger(),
    )
    request = types.SimpleNamespace(behavior_tree="")
    first_response = module.NavigateToPoseRouter._goal_callback(router, request)
    module.NavigateToPoseRouter.set_current_mode(router, "switching")
    second_response = module.NavigateToPoseRouter._goal_callback(router, request)

    assert first_response is module.GoalResponse.ACCEPT
    assert router._current_mode == "switching"
    assert second_response is module.GoalResponse.REJECT
    assert module.NavigateToPoseRouter.get_pending_goal_count(router) == 1
    module.NavigateToPoseRouter._release_goal_reservation(router)
    assert module.NavigateToPoseRouter.get_pending_goal_count(router) == 0
    assert any("rejected" in message.lower() for level, message in logs if level == "warn")


def test_queued_dds_mode_cannot_reopen_admission_after_authoritative_switching(monkeypatch):
    module = _load_router_for_host_test(monkeypatch)
    logs = []

    class Logger:
        def debug(self, message):
            logs.append(message)

        def info(self, _message):
            pass

        def warn(self, _message):
            pass

        def error(self, _message):
            pass

    router = module.NavigateToPoseRouter.__new__(module.NavigateToPoseRouter)
    router._admission_lock = threading.Lock()
    router._current_mode = "navigation"
    router._has_authoritative_mode_source = False
    router._pending_goal_reservations = 0
    router.get_logger = lambda: Logger()

    # The direct sink closes admission, then an older navigation sample that
    # was already queued in DDS arrives.  It must not reopen the route.
    router.set_current_mode("switching")
    router._mode_callback(types.SimpleNamespace(data="navigation"))
    response = router._goal_callback(types.SimpleNamespace(behavior_tree=""))

    assert router._current_mode == "switching"
    assert router._has_authoritative_mode_source is True
    assert response is module.GoalResponse.REJECT
    assert router.get_pending_goal_count() == 0
    assert any("Ignoring DDS mode update" in message for message in logs)


def test_cancel_terminal_budget_starts_after_slow_cancel_rpc_acknowledgement():
    tree = ast.parse(MODE_MANAGER.read_text())
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_cancel_active_navigation"
    )
    method_module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(method_module)

    class FakeClock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, duration):
            cls.now += duration
            if cls.now >= 100.1:
                manager._nav_active_goals = 0

    class CancelGoal:
        class Request:
            pass

    class Logger:
        pass

    manager = types.SimpleNamespace(
        _nav_active_goals=1,
        _service_clients={},
        _navigation_pending_goals=lambda: 0,
        get_logger=lambda: Logger(),
    )

    def slow_cancel_rpc(_clients, _logger, service_name, request, timeout_sec):
        assert service_name == "/internal_navigate_to_pose/_action/cancel_goal"
        assert isinstance(request, CancelGoal.Request)
        assert timeout_sec == 2.0
        # Consume far more than the terminal budget before acknowledging.  A
        # deadline created before this call would already be expired.
        FakeClock.now = 100.0
        return types.SimpleNamespace(goals_canceling=[object()])

    namespace = {
        "CancelGoal": CancelGoal,
        "NAV_CANCEL_SERVICE": "/internal_navigate_to_pose/_action/cancel_goal",
        "call_service": slow_cancel_rpc,
        "time": FakeClock,
    }
    exec(compile(method_module, str(MODE_MANAGER), "exec"), namespace)

    success, message = namespace["_cancel_active_navigation"](manager, rpc_timeout_sec=2.0, terminal_timeout_sec=0.2)

    assert success is True
    assert "Cancelled 1 navigation goal" in message
    assert FakeClock.now >= 100.1


@pytest.mark.parametrize(
    ("goals_canceling", "terminal_status", "cancel_accepted"),
    [([object()], 5, True), ([], 4, False)],
)
def test_handoff_cancel_waits_for_ack_and_terminal_result(
    monkeypatch, goals_canceling, terminal_status, cancel_accepted
):
    module = _load_router_for_host_test(monkeypatch)
    events = []
    logs = []
    terminal_response = types.SimpleNamespace(status=terminal_status, result=object())

    class Logger:
        def info(self, message):
            logs.append(("info", message))

        def error(self, message):
            logs.append(("error", message))

    class RouterHarness:
        def get_logger(self):
            return Logger()

    class ClientGoalHandle:
        def get_result_async(self):
            events.append("result_registered")

            async def wait_for_terminal():
                events.append("terminal_result")
                return terminal_response

            return wait_for_terminal()

        def cancel_goal_async(self):
            events.append("cancel_requested")

            async def wait_for_ack():
                events.append("cancel_ack")
                return types.SimpleNamespace(goals_canceling=goals_canceling)

            return wait_for_ack()

    outcome = asyncio.run(
        module.NavigateToPoseRouter._cancel_internal_and_wait_for_terminal(RouterHarness(), ClientGoalHandle())
    )

    assert events == ["result_registered", "cancel_requested", "cancel_ack", "terminal_result"]
    assert outcome == (cancel_accepted, terminal_response)
    if cancel_accepted:
        assert not [message for level, message in logs if level == "error"]
    else:
        assert any("rejected" in message for level, message in logs if level == "error")
