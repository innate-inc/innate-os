"""Recorder -> demonstration context -> actual skill execution with fake actuators."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in ("ros2_ws/src/brain/brain_client", "ros2_ws/src/mars_bot/mars_arm", "workspace"):
    sys.path.insert(0, str(ROOT / directory))

from innate_skills import imitate_pick_and_present as runtime  # noqa: E402

from innate.exceptions import SkillCancelled, SkillFailed  # noqa: E402
from innate.gesture import Gesture, GesturePolicy, convert_recording, forward_poses, validate_action  # noqa: E402

URDF = ROOT / "ros2_ws/src/mars_bot/mars_sim/urdf/mars.urdf"


@pytest.fixture
def episode(tmp_path):
    binary = os.environ.get("GESTURE_RECORDER_FIXTURE")
    if not binary:
        pytest.skip("Set GESTURE_RECORDER_FIXTURE to the compiled record_ee_fixture executable")
    path = tmp_path / "episode.h5"
    subprocess.run([binary, str(URDF), str(path)], check=True)
    return path


def test_cpp_recording_matches_arm_fk_and_preserves_layout(episode):
    with h5py.File(episode) as f:
        ds = f["observations/ee_pose"]
        names = ds.attrs["joint_names"].decode().split(",")
        expected = forward_poses(URDF.read_text(), names, f["observations/qpos"][:])
        np.testing.assert_allclose(ds[:], expected, atol=1e-10)
        assert ds.shape == (5, 7)  # Sixth failed append rolled back all datasets.
        assert f["action"].shape == (5, 10)
        assert f["observations/images/camera_1"].shape[0] == 5
        np.testing.assert_allclose(f["action"][:, -2], np.linspace(0, 1, 5))
    demo = Gesture(episode)
    assert len(demo.frames) == 5
    assert len([c for c in demo.context() if c["type"] == "input_image"]) == 10


def test_legacy_conversion_keeps_original_and_uses_measured_joints(episode, tmp_path):
    with h5py.File(episode, "r+") as f:
        names = f["observations/ee_pose"].attrs["joint_names"].decode().split(",")
        q = f["observations/qpos"][:]
        f["observations/qpos"][:] = q[:, [names.index(f"joint{i}") for i in range(1, 7)]]
        del f["observations/ee_pose"]
    before = episode.read_bytes()
    out = convert_recording(episode, tmp_path / "converted.h5", URDF)
    assert episode.read_bytes() == before
    with h5py.File(out) as f:
        assert f["observations/ee_pose"].shape == (5, 7)
    with pytest.raises(FileExistsError):
        convert_recording(episode, out, URDF)
    with pytest.raises(ValueError, match="explicitly supply"):
        Gesture(episode)
    assert Gesture(episode, legacy_urdf=URDF).model_hash == Gesture(out).model_hash


def decision(action="move", x=0.30, z=0.20):
    return dict(
        action=action,
        reference_frame=1,
        pose=[x, 0.0, z, 0.0, 0.0, 0.0],
        reason="Adapt to the demonstrated grasp",
        safe=True,
        holding=True,
        presented=True,
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(pose=[float("nan")] * 6),
        dict(pose=[0.5, 0, 0.2, 0, 0, 0]),
        dict(reference_frame=-1),
        dict(safe=False),
        dict(action="exec"),
    ],
)
def test_reject_invalid_model_action(change):
    value = decision()
    value.update(change)
    with pytest.raises(ValueError):
        validate_action(value, [0.3, 0, 0.2, 0, 0, 0], 5, False)


def test_different_targets_and_release_guard():
    a, b = decision(x=0.28), decision(x=0.32)
    assert (
        validate_action(a, [0.3, 0, 0.2, 0, 0, 0], 5, False)["pose"]
        != validate_action(b, [0.3, 0, 0.2, 0, 0, 0], 5, False)["pose"]
    )
    with pytest.raises(ValueError, match="release"):
        validate_action(decision("open"), [0.3, 0, 0.2, 0, 0, 0], 5, True)


def test_stale_recorded_camera_rejected(episode):
    with h5py.File(episode, "r+") as f:
        f["timestamps/images/camera_2"][0] = 800.0
    with pytest.raises(ValueError, match="synchronized"):
        Gesture(episode)


def test_model_receives_demo_and_live_images_every_turn(episode):
    demo = Gesture(episode)
    policy = GesturePolicy.__new__(GesturePolicy)
    policy.demonstration = demo.context()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def read(self):
            return json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(decision())}]}
                    ],
                }
            )

    def request(*args, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    policy.client = SimpleNamespace(request_stream=request)
    for x in (0.28, 0.32):
        policy.decide({"pose": [x, 0, 0.2, 0, 0, 0], "images": demo.frames[0]["images"]}, [])
    for request in requests:
        assert request["model"] == "gpt-6-astra"
        assert len([p for p in request["input"][0]["content"] if p["type"] == "input_image"]) == 12
    assert requests[0]["input"] != requests[1]["input"]


@pytest.mark.parametrize("mode", ["success", "cancel_after_close", "unreachable", "model_failure"])
def test_real_skill_loop_preserves_grip_and_stops_on_failure(episode, tmp_path, monkeypatch, mode):
    import ament_index_python.packages

    monkeypatch.setattr(ament_index_python.packages, "get_package_share_directory", lambda _: str(URDF.parent.parent))
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    # Use real recorded context, but make the demonstrated final pose match this fixture's presentation.
    real_gesture = Gesture

    def gesture(*args, **kwargs):
        demo = real_gesture(*args, **kwargs)
        demo.final_pose[:3] = [0.3, 0, 0.26]
        return demo

    monkeypatch.setattr(runtime, "Gesture", gesture)
    commands = []
    current = dict(pose=[0.3, 0, 0.20, 0, 0, 0], base=[0, 0, 0], images={"head": "", "wrist": ""}, gripper=1.0)

    class Arm:
        safety = SimpleNamespace(max_ee_speed=None)
        moving = False

        def reachable(self, *args, **kwargs):
            return mode != "unreachable"

        def move_to(self, x, y, z, **kwargs):
            commands.append("move")
            current["pose"][:3] = [x, y, z]

        def gripper_open(self, **kwargs):
            commands.append("open")

        def gripper_close(self, **kwargs):
            commands.append("close")

        def halt(self):
            commands.append("halt")

        def wait(self, **kwargs):
            pass

    sequence = iter(
        [
            decision("open"),
            decision("close"),
            decision(z=0.23),
            decision(z=0.26),
            decision("done", z=0.26),
            decision("done", z=0.26),
        ]
    )
    monkeypatch.setattr(runtime, "GesturePolicy", lambda _: object())
    monitor = SimpleNamespace(close=lambda: commands.append("monitor_closed"))
    monkeypatch.setattr(runtime, "LiveGestureObservation", lambda: monitor)
    skill = runtime.ImitatePickAndPresent(None)
    skill.head_position = SimpleNamespace(pitch_degrees=25.0)
    skill.manipulation = Arm()
    skill.mobility = SimpleNamespace(stop=lambda: commands.append("base_stop"))
    skill._observe = lambda *args: copy.deepcopy(current)

    def decide(*args):
        if mode == "unreachable":
            return decision(z=0.23)
        if mode == "model_failure":
            raise SkillFailed("model failed")
        if mode == "cancel_after_close" and "close" in commands:
            raise SkillCancelled()
        return next(sequence)

    skill._decide = decide
    skill.sleep = lambda _: None
    skill.feedback = lambda _: None
    if mode == "success":
        result = skill.execute(str(episode), "cup")
        assert result.data["released"] is False
        assert commands.count("move") == 2
    else:
        with pytest.raises((SkillFailed, SkillCancelled)):
            skill.execute(str(episode), "cup")
    if "close" in commands:
        assert "open" not in commands[commands.index("close") + 1 :]
    assert commands[-3:] == ["halt", "base_stop", "monitor_closed"]
    assert skill.manipulation.safety.max_ee_speed is None


def test_ros_observation_rejects_repeated_frames_and_base_motion():
    import time

    import rclpy
    from mars_msgs.msg import ArmStatus
    from nav_msgs.msg import Odometry
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage, JointState

    rclpy.init()
    publisher = rclpy.create_node("gesture_test_publisher")
    monitor = runtime.LiveGestureObservation()
    topics = [
        (CompressedImage, "/mars/main_camera/left/image_raw/compressed"),
        (CompressedImage, "/mars/arm/image_raw/compressed"),
        (JointState, "/mars/arm/state"),
        (Odometry, "/odom"),
        (ArmStatus, "/mars/arm/status"),
    ]
    pubs = [publisher.create_publisher(t, topic, qos_profile_sensor_data) for t, topic in topics]
    messages = [t() for t, _ in topics]
    messages[0].data = [1, 2, 3]
    messages[1].data = [1, 2, 3]
    messages[2].name = [f"joint{i}" for i in range(1, 7)]
    messages[2].position = [0.1, -0.5, 0.4, 0.8, 0.0, 1.0]
    messages[3].pose.pose.orientation.w = 1.0
    messages[4].is_ok = True
    messages[4].is_torque_enabled = True

    def publish(fresh=True):
        stamp = publisher.get_clock().now().to_msg()
        for pub, msg in zip(pubs, messages, strict=True):
            if fresh and hasattr(msg, "header"):
                msg.header.stamp = stamp
            pub.publish(msg)
        time.sleep(0.05)

    try:
        start = time.monotonic()
        observation = None
        while time.monotonic() - start < 5 and observation is None:
            publish()
            observation = monitor.snapshot(start, URDF.read_text())
        assert observation is not None
        after = time.monotonic()
        publish(fresh=False)
        assert monitor.snapshot(after, URDF.read_text()) is None
        messages[3].twist.twist.linear.x = 0.1
        publish()
        with pytest.raises(ValueError, match="Base moved"):
            monitor.snapshot(after, URDF.read_text())
    finally:
        monitor.close()
        publisher.destroy_node()
        rclpy.shutdown()


def test_recorder_services_save_ee_pose_with_camera_rows(tmp_path):
    import time

    import rclpy
    from brain_messages.srv import ActivateManipulationTask, NewEpisode
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import Float64MultiArray
    from std_srvs.srv import Trigger

    binary = os.environ.get("GESTURE_RECORDER_NODE")
    if not binary:
        pytest.skip("Set GESTURE_RECORDER_NODE to the built recorder_node_cpp")
    task = tmp_path / "gesture"
    task.mkdir()
    (task / "metadata.json").write_text("{}")
    command = [
        binary,
        "--ros-args",
        "-p",
        f"data_directory:={tmp_path}",
        "-p",
        "image_size:=[32,24]",
        "-p",
        "image_topics:=['/mars/main_camera/left/image_raw','/mars/arm/image_raw']",
        "-p",
        "arm_state_topic:=/mars/arm/state",
        "-p",
        "leader_command_topic:=/mars/arm/commands",
    ]
    rclpy.init()
    node = rclpy.create_node("gesture_recorder_test")
    types_topics = [
        (Image, "/mars/main_camera/left/image_raw"),
        (Image, "/mars/arm/image_raw"),
        (JointState, "/mars/arm/state"),
        (Float64MultiArray, "/mars/arm/commands"),
        (Twist, "/cmd_vel"),
        (Odometry, "/odom"),
    ]
    pubs = [node.create_publisher(t, topic, 10) for t, topic in types_topics]
    messages = [t() for t, _ in types_topics]
    for image in messages[:2]:
        image.height, image.width, image.step, image.encoding = 24, 32, 96, "bgr8"
        image.data = [40] * (24 * 32 * 3)
    messages[2].name = [f"joint{i}" for i in range(1, 7)]
    messages[2].position = [0.1, -0.5, 0.4, 0.8, 0.0, 1.0]
    messages[2].velocity = [0.0] * 6
    messages[3].data = list(messages[2].position)
    messages[-1].pose.pose.orientation.w = 1.0

    def publish():
        stamp = node.get_clock().now().to_msg()
        for pub, msg in zip(pubs, messages, strict=True):
            if hasattr(msg, "header"):
                msg.header.stamp = stamp
            pub.publish(msg)
        rclpy.spin_once(node, timeout_sec=0.03)

    def call(service, name, request):
        client = node.create_client(service, name)
        assert client.wait_for_service(timeout_sec=5)
        future = client.call_async(request)
        deadline = time.monotonic() + 5
        while not future.done() and time.monotonic() < deadline:
            publish()
        assert future.done() and future.result().success

    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        request = ActivateManipulationTask.Request()
        request.task_directory = str(task)
        call(ActivateManipulationTask, "/brain/recorder/activate_physical_primitive", request)
        call(NewEpisode, "/brain/recorder/new_episode", NewEpisode.Request())
        until = time.monotonic() + 0.8
        while time.monotonic() < until:
            publish()
        call(Trigger, "/brain/recorder/save_episode", Trigger.Request())
        recording = task / "data/episode_0.h5"
        demo = Gesture(recording)
        assert len(demo.poses) >= 2
        with h5py.File(recording) as f:
            for key in ("action", "observations/qpos", "observations/ee_pose", "observations/images/camera_1"):
                assert len(f[key]) == len(demo.poses)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        node.destroy_node()
        rclpy.shutdown()


def test_cancel_during_model_request_returns_before_late_response():
    import threading
    import time

    entered, release = threading.Event(), threading.Event()

    class Policy:
        def decide(self, *_):
            entered.set()
            release.wait(timeout=3)
            return decision()

    skill = runtime.ImitatePickAndPresent(None)

    def cancel():
        entered.wait(timeout=2)
        skill._cancel_latch().set()

    thread = threading.Thread(target=cancel)
    thread.start()
    start = time.monotonic()
    try:
        with pytest.raises(SkillCancelled):
            skill._decide(Policy(), {}, [])
        assert time.monotonic() - start < 1
    finally:
        release.set()
        thread.join(timeout=2)
