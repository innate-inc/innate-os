"""Actual ROS request boundary, with no simulator truth in the robot process."""

import argparse
import json
import time
from pathlib import Path

import rclpy
from brain_messages.action import ExecuteSkill
from rclpy.action import ActionClient
from std_srvs.srv import SetBool

p = argparse.ArgumentParser()
p.add_argument("output")
p.add_argument("--prompt", default="the red LEGO brick")
p.add_argument("--controller", choices=("astra", "classic"))
p.add_argument("--timeout", type=float, default=180)
p.add_argument("--disable-only", action="store_true")
p.add_argument("--cancel-during-astra", action="store_true")
a = p.parse_args()
rclpy.init()
node = rclpy.create_node("pickup_measurement")
client = ActionClient(node, ExecuteSkill, "execute_skill")


def wait(future, seconds):
    rclpy.spin_until_future_complete(node, future, timeout_sec=seconds)
    if not future.done():
        raise TimeoutError("ROS operation timed out")
    return future.result()


row = {}
try:
    control = node.create_client(SetBool, "/brain/set_brain_active")
    if not control.wait_for_service(timeout_sec=10):
        raise RuntimeError("Brain control unavailable")
    assert wait(control.call_async(SetBool.Request(data=False)), 10).success
    if a.disable_only:
        row["brain_disabled"] = True
        raise SystemExit(0)
    if not client.wait_for_server(timeout_sec=10):
        raise RuntimeError("Skill action unavailable")
    started = time.monotonic()
    row = {"request_wall": time.time(), "prompt": a.prompt, "timeout_s": a.timeout}
    inputs = {"prompt": a.prompt}
    if a.controller is not None:
        inputs["controller"] = a.controller
    row["inputs"] = inputs
    goal = ExecuteSkill.Goal(skill_type="innate-os/pick_any_object", inputs=json.dumps(inputs))
    handle = wait(client.send_goal_async(goal), 10)
    row["accepted"] = handle.accepted
    if not handle.accepted:
        raise RuntimeError("Pickup rejected")
    result_future = handle.get_result_async()
    try:
        if a.cancel_during_astra:
            probe = Path("/root/innate-os/workspace/skill_storage/pickup_probe/events.jsonl")
            while not result_future.done() and time.monotonic() - started < a.timeout:
                rclpy.spin_once(node, timeout_sec=0.05)
                events = [json.loads(line) for line in probe.read_text().splitlines()] if probe.exists() else []
                if any(
                    e["kind"] == "provider_start"
                    and e.get("model") == "gpt-6-astra"
                    and e["wall"] >= row["request_wall"]
                    for e in events
                ):
                    row["cancel_wall"] = time.time()
                    cancelled = wait(handle.cancel_goal_async(), 15)
                    row["cancel_acknowledged"] = bool(cancelled.goals_canceling)
                    break
        result = wait(result_future, max(0, a.timeout - (time.monotonic() - started)))
        row.update(
            success=result.result.success,
            success_type=result.result.success_type,
            message=result.result.message,
            status=result.status,
        )
    except TimeoutError:
        row["timed_out"] = True
        wait(handle.cancel_goal_async(), 15)
        # Committed lift/carry may finish after cancel; retain its result.
        result = wait(result_future, 45)
        row.update(
            success=False,
            success_type=result.result.success_type,
            message=result.result.message,
            status=result.status,
        )
    row.update(completion_wall=time.time(), action_elapsed_s=time.monotonic() - started)
except Exception as error:
    if "request_wall" not in row:
        raise  # preflight failure is not a submitted pickup attempt
    row.update(
        success=False,
        runner_error=type(error).__name__,
        message="Pickup rejected" if row.get("accepted") is False else "Runner failed after submission",
        completion_wall=time.time(),
        action_elapsed_s=time.monotonic() - started,
    )
finally:
    Path(a.output).write_text(json.dumps(row, indent=2) + "\n")
    print(json.dumps(row), flush=True)
    node.destroy_node()
    rclpy.shutdown()
