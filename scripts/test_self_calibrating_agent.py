#!/usr/bin/env python3
"""
Test harness for the CalibratingSelfCalibratingAgent.

Orchestrates:
1. Set directive to calibrating_self_calibrating_agent
2. Reset brain state
3. Activate the brain
4. Monitor /brain/chat_out for agent progress
5. After timeout or completion, deactivate brain
6. Report captured images and results
"""

import subprocess
import sys
import time
import signal
import os
import json
import threading
from pathlib import Path
from datetime import datetime

AGENT_ID = "calibrating_self_calibrating_agent"
CAPTURES_DIR = Path("/home/jetson1/innate-os/captures")
CALIBRATION_FILE = Path.home() / "board_calibration.json"
LOG_FILE = Path("/home/jetson1/innate-os/captures/test_run.log")
TIMEOUT_SECONDS = 120  # 2 minutes max


def ros2_service_call(service, srv_type, args="{}", timeout=30):
    """Call a ROS2 service and return output."""
    cmd = ["ros2", "service", "call", service, srv_type, args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout + result.stderr


def ros2_topic_pub_once(topic, msg_type, data):
    """Publish a single message to a topic."""
    cmd = ["ros2", "topic", "pub", "--once", topic, msg_type, data]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.stdout + result.stderr


def get_existing_captures():
    """Get set of existing capture files."""
    if not CAPTURES_DIR.exists():
        return set()
    return set(str(p) for p in CAPTURES_DIR.glob("contact_*.jpg"))


def monitor_chat_out(stop_event, messages):
    """Monitor /brain/chat_out in a background thread."""
    proc = subprocess.Popen(
        ["ros2", "topic", "echo", "/brain/chat_out", "std_msgs/msg/String", "--no-arr"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while not stop_event.is_set():
            line = proc.stdout.readline()
            if line:
                line = line.strip()
                if line and line != "---" and not line.startswith("stamp:") and "data:" in line:
                    msg = line.replace("data: ", "").strip("'\"")
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    entry = f"[{timestamp}] {msg}"
                    messages.append(entry)
                    print(f"  📡 {entry}")
    finally:
        proc.terminate()
        proc.wait()


def main():
    print("=" * 60)
    print("  Self-Calibrating Agent Test Harness")
    print("=" * 60)

    # Record existing captures before test
    existing_captures = get_existing_captures()
    print(f"\n📁 Existing captures: {len(existing_captures)}")

    # Step 1: Reload skills and agents to pick up code changes
    print("\n🔄 Step 1: Reloading skills and agents...")
    try:
        reload_result = subprocess.run(
            ["python3", "-c", """
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger
rclpy.init()
node = Node('reload_caller')
client = node.create_client(Trigger, '/brain/reload')
if client.wait_for_service(timeout_sec=10.0):
    future = client.call_async(Trigger.Request())
    rclpy.spin_until_future_complete(node, future, timeout_sec=30.0)
    r = future.result()
    print(f'OK: {r.message}' if r and r.success else 'FAILED')
else:
    print('SERVICE NOT AVAILABLE')
node.destroy_node()
rclpy.shutdown()
"""],
            capture_output=True, text=True, timeout=45, env={**os.environ}
        )
        print(f"   {reload_result.stdout.strip()}")
    except Exception as e:
        print(f"   Warning: reload failed ({e})")
    time.sleep(3.0)

    # Step 2: Set directive
    print(f"\n🔧 Step 2: Setting directive to '{AGENT_ID}'...")
    result = ros2_topic_pub_once(
        "/brain/set_directive", "std_msgs/msg/String", f'{{data: "{AGENT_ID}"}}'
    )
    print(f"   Result: directive set")
    time.sleep(1.0)

    # Step 3: Reset brain
    print("\n🔄 Step 3: Resetting brain...")
    result = ros2_service_call("/brain/reset_brain", "brain_messages/srv/ResetBrain")
    print(f"   Result: brain reset")
    time.sleep(1.0)

    # Step 3: Start monitoring chat_out
    print("\n📡 Step 3: Starting chat monitor...")
    stop_event = threading.Event()
    messages = []
    monitor_thread = threading.Thread(
        target=monitor_chat_out, args=(stop_event, messages), daemon=True
    )
    monitor_thread.start()

    # Step 4: Activate brain
    print(f"\n🧠 Step 4: Activating brain (timeout: {TIMEOUT_SECONDS}s)...")
    result = ros2_service_call(
        "/brain/set_brain_active", "std_srvs/srv/SetBool", "{data: true}"
    )
    print(f"   Result: brain activated")
    print("\n⏳ Waiting for agent to run...\n")

    # Step 5: Wait for timeout, checking for new captures periodically
    start_time = time.time()
    last_capture_count = 0
    try:
        while time.time() - start_time < TIMEOUT_SECONDS:
            time.sleep(5)
            elapsed = int(time.time() - start_time)

            # Check for new captures
            current_captures = get_existing_captures()
            new_captures = current_captures - existing_captures
            if len(new_captures) > last_capture_count:
                print(f"  📸 New capture detected! ({len(new_captures)} total new images)")
                last_capture_count = len(new_captures)

            # Print status every 30s
            if elapsed % 30 == 0:
                print(f"  ⏱️  {elapsed}s elapsed, {len(messages)} messages, {len(new_captures)} new captures")

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")

    # Step 6: Deactivate brain
    print("\n\n🛑 Step 6: Deactivating brain...")
    result = ros2_service_call(
        "/brain/set_brain_active", "std_srvs/srv/SetBool", "{data: false}"
    )
    print(f"   Result: brain deactivated")

    # Stop monitor
    stop_event.set()
    monitor_thread.join(timeout=3)

    # Step 7: Report results
    print("\n" + "=" * 60)
    print("  TEST RESULTS")
    print("=" * 60)

    # Chat messages
    print(f"\n💬 Chat messages ({len(messages)}):")
    for msg in messages:
        print(f"   {msg}")

    # New captures
    current_captures = get_existing_captures()
    new_captures = sorted(current_captures - existing_captures)
    print(f"\n📸 New captures ({len(new_captures)}):")
    for cap in new_captures:
        print(f"   {cap}")

    # Calibration file
    if CALIBRATION_FILE.exists():
        cal = json.loads(CALIBRATION_FILE.read_text())
        print(f"\n📐 Calibration file ({CALIBRATION_FILE}):")
        print(f"   {json.dumps(cal, indent=2)}")

    # Save log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "w") as f:
        f.write(f"Test run: {datetime.now().isoformat()}\n")
        f.write(f"Agent: {AGENT_ID}\n")
        f.write(f"Duration: {int(time.time() - start_time)}s\n")
        f.write(f"\nMessages:\n")
        for msg in messages:
            f.write(f"  {msg}\n")
        f.write(f"\nNew captures:\n")
        for cap in new_captures:
            f.write(f"  {cap}\n")
    print(f"\n📝 Log saved to: {LOG_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
