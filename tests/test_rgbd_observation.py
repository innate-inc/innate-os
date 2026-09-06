import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

path = Path(__file__).resolve().parents[1] / "ros2_ws/src/brain/brain_client/brain_client/perception/rgbd.py"
spec = importlib.util.spec_from_file_location("rgbd_under_test", path)
rgbd = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rgbd
spec.loader.exec_module(rgbd)


def messages():
    def header():
        return NS(stamp=NS(sec=10, nanosec=0), frame_id="camera_optical_frame")

    rgb = NS(header=header(), data=cv2.imencode(".jpg", np.zeros((300, 400), np.uint8))[1].tobytes())
    depth = NS(
        header=header(),
        height=150,
        width=200,
        encoding="16UC1",
        is_bigendian=True,
        step=404,
        data=(np.full((150, 202), 1200, dtype=">u2")).tobytes(),
    )
    rotation = cv2.Rodrigues(np.array([0.0, 0.06, 0.0]))[0]
    info = NS(
        header=header(),
        width=800,
        height=600,
        distortion_model="plumb_bob",
        k=[510.0, 0.0, 398.0, 0.0, 500.0, 299.0, 0.0, 0.0, 1.0],
        d=[0.1, -0.03, 0.002, -0.001, 0.0],
        r=rotation.ravel(),
        p=[520.0, 0.0, 401.0, 0.0, 0.0, 515.0, 301.0, 0.0, 0.0, 0.0, 1.0, 0.0],
        binning_x=0,
        binning_y=0,
        roi=NS(x_offset=0, y_offset=0, width=0, height=0, do_rectify=False),
    )
    return rgb, depth, info


def build(msgs, **kwargs):
    return rgbd.RgbdObservation.from_messages(
        *msgs, (20.0, 20.0, 20.0), now_ns=10_100_000_000, now_monotonic=20.1, **kwargs
    )


def test_distorted_raw_pixel_to_rectified_axial_depth_and_back_with_resize_and_padding():
    msgs = messages()
    observation = build(msgs)
    rectified = np.array([0.12, -0.06, 1.2])
    expected = np.reshape(msgs[2].r, (3, 3)).T @ rectified
    raw, _ = cv2.projectPoints(
        expected[None], np.zeros(3), np.zeros(3), np.reshape(msgs[2].k, (3, 3)), np.array(msgs[2].d)
    )
    point = observation.surface_point(*(raw.reshape(2) / 2))
    np.testing.assert_allclose(point, expected, atol=1e-6)
    assert observation.depth_m[0, 0] == pytest.approx(1.2)
    with pytest.raises(ValueError):
        observation.depth_m.setflags(write=True)


@pytest.mark.parametrize(
    "failure",
    [
        "skew",
        "stale",
        "future",
        "frame",
        "calibration",
        "rotation",
        "roi",
        "truncated",
        "stride",
        "distortion",
        "invalid_jpeg",
    ],
)
def test_invalid_association_or_metadata_fails_closed(failure):
    rgb, depth, info = messages()
    if failure == "skew":
        depth.header.stamp.nanosec = 1
    if failure == "stale":
        for m in (rgb, depth, info):
            m.header.stamp.sec = 9
    if failure == "future":
        for m in (rgb, depth, info):
            m.header.stamp.sec = 11
    if failure == "frame":
        depth.header.frame_id = "other"
    if failure == "calibration":
        info.k[0] = 0
    if failure == "rotation":
        info.r = np.zeros(9)
    if failure == "roi":
        info.roi.x_offset = 1
    if failure == "truncated":
        depth.data = depth.data[:-1]
    if failure == "stride":
        depth.step = 399
    if failure == "distortion":
        info.distortion_model = "equidistant"
    if failure == "invalid_jpeg":
        rgb.data = b"bad"
    assert build((rgb, depth, info)) is None


def test_surface_rejects_missing_depth_discontinuity_and_out_of_frame():
    rgb, depth, info = messages()
    for value in (0.0, float("nan")):
        depth.encoding, depth.is_bigendian, depth.step = "32FC1", False, 800
        depth.data = np.full((150, 200), value, np.float32).tobytes()
        assert build((rgb, depth, info)).surface_point(200, 150) is None
    depth.data = np.tile(np.array([1.0, 2.0], np.float32), (150, 100)).tobytes()
    observation = build((rgb, depth, info))
    assert observation.surface_point(200, 150) is None
    assert observation.surface_point(-1, 150) is None
    assert observation.surface_point(float("nan"), 150) is None


def test_recent_delivery_does_not_make_old_frame_fresh():
    assert (
        rgbd.RgbdObservation.from_messages(*messages(), (19.0, 20.0, 20.0), now_ns=10_100_000_000, now_monotonic=20.1)
        is None
    )


def test_provider_association_and_calibration_invalidation_with_ros_messages():
    rclpy = pytest.importorskip("rclpy")
    from sensor_msgs.msg import CameraInfo, CompressedImage, Image

    from brain_client.perception.camera_provider import CameraProvider

    rclpy.init()
    provider = CameraProvider()
    try:
        source = messages()
        rgb, depth, info = CompressedImage(), Image(), CameraInfo()
        stamp = provider.get_clock().now().to_msg()
        for msg in (rgb, depth, info):
            msg.header.stamp = stamp
            msg.header.frame_id = "camera_optical_frame"
        rgb.data = source[0].data
        for field in ("height", "width", "encoding", "is_bigendian", "step", "data"):
            setattr(depth, field, getattr(source[1], field))
        for field in ("width", "height", "distortion_model"):
            setattr(info, field, getattr(source[2], field))
        for field in ("k", "d", "r", "p"):
            setattr(info, field, list(getattr(source[2], field)))
        provider._main_camera_cb(rgb)
        provider._depth_cb(depth)
        provider._info_cb(info)
        observation = provider.rgbd_observation()
        assert observation is not None
        assert provider.observation_is_current(observation)
        from geometry_msgs.msg import TransformStamped
        from rclpy.time import Time

        from brain_client.perception.camera_provider import _CaptureTransforms

        provider._tf_buffer = _CaptureTransforms(lambda: provider._rgbd_capture_after_ns)
        static = TransformStamped()
        static.header.frame_id, static.child_frame_id = "head", "camera_optical_frame"
        static.transform.rotation.w = 1.0
        static.transform.translation.x = 0.03
        provider._tf_buffer.set_transform_static(static, "test")
        odom = TransformStamped()
        odom.header.frame_id, odom.child_frame_id = "odom", "base_link"
        odom.transform.rotation.w = 1.0
        odom.transform.translation.x = 4.0
        provider._tf_buffer.set_transform_static(odom, "test")
        for dt, x in ((-100_000_000, 0.0), (100_000_000, 2.0)):
            transform = TransformStamped()
            transform.header.frame_id, transform.child_frame_id = "base_link", "head"
            transform.header.stamp = Time(nanoseconds=observation.stamp_ns + dt).to_msg()
            transform.transform.rotation.w = 1.0
            transform.transform.translation.x = x
            provider._tf_buffer.set_transform(transform, "test")
        posed = provider.rgbd_observation(require_pose=True)
        assert posed.odom_from_optical[0] == pytest.approx(5.03)
        assert posed.base_from_optical[0] == pytest.approx(1.03)  # capture-time interpolation, not latest2.03
        from std_msgs.msg import Int64

        provider._epoch_cb(Int64(data=1))
        assert not provider.observation_is_current(observation)
        transform.header.stamp = Time(nanoseconds=observation.stamp_ns - 100_000_000).to_msg()
        provider._tf_buffer.set_transform(transform, "delayed pre-reset")
        assert not provider._tf_buffer.can_transform("base_link", "camera_optical_frame", Time())
        assert provider._tf_buffer.can_transform("head", "camera_optical_frame", Time())
        provider._main_camera_cb(rgb)
        provider._depth_cb(depth)
        provider._info_cb(info)
        assert provider.rgbd_observation() is None  # delayed pre-reset delivery
        stamp = provider.get_clock().now().to_msg()
        for msg in (rgb, depth, info):
            msg.header.stamp = stamp
        provider._main_camera_cb(rgb)
        provider._depth_cb(depth)
        provider._info_cb(info)
        observation = provider.rgbd_observation()
        assert observation is not None
        assert provider.rgbd_observation(require_pose=True) is None  # no post-reset dynamic TF
        # A newly received uncalibrated CameraInfo invalidates the older triplet.
        newer = CameraInfo()
        newer.header.stamp = provider.get_clock().now().to_msg()
        newer.header.frame_id = info.header.frame_id
        provider._info_cb(newer)
        assert provider.rgbd_observation() is None
        assert not provider.observation_is_current(observation)
    finally:
        provider.destroy_node()
        rclpy.shutdown()


def test_paired_render_protocol_preserves_capture_stamp_and_rejects_truncation():
    remote_path = (
        Path(__file__).resolve().parents[1] / "ros2_ws/src/mars_bot/mars_sim_driver/mars_sim_driver/remote_world.py"
    )
    remote_spec = importlib.util.spec_from_file_location("rgbd_remote_test", remote_path)
    remote = importlib.util.module_from_spec(remote_spec)
    remote_spec.loader.exec_module(remote)
    blob = b"jpeg" + np.full((2, 3), 1.2, dtype="<f4").tobytes()
    meta = dict(shape=[2, 3], dtype="<f4", jpeg_size=4, captured_ns=100)
    world = object.__new__(remote.RemoteWorld)
    world._render_ch = NS(call=lambda request: (meta, blob))
    jpeg, depth, stamp = world.render_rgbd("main")
    assert jpeg == b"jpeg" and stamp == 100
    np.testing.assert_allclose(depth, 1.2)
    assert world.render_rgbd("main")[2] == 100  # cached bytes retain capture time
    blob = blob[:-1]
    with pytest.raises(RuntimeError, match="malformed"):
        world.render_rgbd("main")


def test_optical_surface_to_base_uses_capture_transform_and_rejects_invalid_pose():
    observation = build(messages())
    raw = np.array(observation.surface_point(200, 150))
    q = np.sqrt(0.5)
    posed = replace(observation, base_from_optical=(0.1, 0.2, 0.3, 0.0, 0.0, q, q))
    expected = np.array([-raw[1], raw[0], raw[2]]) + [0.1, 0.2, 0.3]
    np.testing.assert_allclose(posed.surface_point_in_base(200, 150), expected, atol=1e-6)
    assert observation.surface_point_in_base(200, 150) is None
    assert replace(posed, base_from_optical=(0.0,) * 7).surface_point_in_base(200, 150) is None


def test_sdk_optional_rgbd_annotation_declares_the_shared_feed():
    pytest.importorskip("rclpy")
    from brain_client.skills.types import RobotStateType
    from innate import RgbdObservation, Skill

    class Probe(Skill):
        rgbd: RgbdObservation | None

        def execute(self):
            pass

    probe = Probe(logger=None)
    assert RobotStateType.LAST_RGBD_OBSERVATION in probe.declared_robot_state_types()
    assert not Probe.rgbd.required
    from brain_client.skills.robot_state import RobotStateProvider

    provider = object.__new__(RobotStateProvider)
    provider._warn_missing = lambda *_: None
    feed = RobotStateType.LAST_RGBD_OBSERVATION
    observation = build(messages())
    provider._inject(probe, [(feed, lambda: observation)])
    assert probe.rgbd is observation
    provider._inject(probe, [(feed, lambda: None)])
    assert probe.rgbd is None
