import sys
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
from innate_skills import pickup_rgbd
from innate_skills.pickup_rgbd import compact_upper_surface, revalidate_material_point, same_wrist_patch
from test_rgbd_observation import messages, rgbd


@pytest.fixture(autouse=True)
def receipt_clock(monkeypatch):
    monkeypatch.setattr(pickup_rgbd.time, "monotonic", lambda: 20.1)


BOX = [300, 350, 700, 650]
POINT = [500, 500]


def scene(shift=0, occlude=False, exposure=0):
    a = np.full((300, 400, 3), 40, np.uint8)
    a[90:210, 140 + shift : 260 + shift] = [90, 140, 190]
    # A visible material detail prevents this test only checking silhouettes.
    a[145:156, 195 + shift : 206 + shift] = [150, 80, 30]
    if occlude:
        a[145:156, 195:206] = [10, 10, 10]
    return np.clip(a.astype(int) + exposure, 0, 255).astype(np.uint8)


def snapshot(stamp, image, depth_value=1200):
    rgb, depth, info = messages()
    for m in (rgb, depth, info):
        m.header.stamp.sec = stamp
    rgb.data = cv2.imencode(".jpg", image)[1].tobytes()
    depth.data = np.full((150, 202), depth_value, dtype=">u2").tobytes()
    o = rgbd.RgbdObservation.from_messages(
        rgb, depth, info, (20.0, 20.0, 20.0), now_ns=stamp * 10**9 + 100_000_000, now_monotonic=20.1
    )
    return replace(
        o, base_from_optical=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0), odom_from_optical=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    )


def verify(a, b):
    return revalidate_material_point(a, b, BOX, POINT, now_ns=13_100_000_000)


def test_historical_model_frame_requires_and_uses_fresh_verified_surface():
    a = snapshot(10, scene())
    b = snapshot(13, scene(exposure=5), 1205)
    point = verify(a, b)
    np.testing.assert_allclose(point, b.surface_point_in_base(200, 150))
    assert point != a.surface_point_in_base(200, 150)


@pytest.mark.parametrize(
    "case",
    [
        "same_frame",
        "stale",
        "reset",
        "no_pose",
        "camera_moved",
        "calibration",
        "object_moved",
        "occlusion",
        "missing_depth",
        "depth_changed",
    ],
)
def test_rejects_unverified_current_target(case):
    a = snapshot(10, scene())
    b = snapshot(13, scene())
    if case == "same_frame":
        b = a
    elif case == "stale":
        b = replace(b, stamp_ns=12_000_000_000)
    elif case == "reset":
        b = replace(b, generation=1)
    elif case == "no_pose":
        b = replace(b, odom_from_optical=None)
    elif case == "camera_moved":
        b = replace(b, odom_from_optical=(0.003, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0))
    elif case == "calibration":
        b = replace(b, k=(b.k[0] + 1, *b.k[1:]))
    elif case == "object_moved":
        b = snapshot(13, scene(shift=12))
    elif case == "occlusion":
        b = snapshot(13, scene(occlude=True))
    elif case == "missing_depth":
        b = snapshot(13, scene(), 0)
    elif case == "depth_changed":
        b = snapshot(13, scene(), 1250)
    assert verify(a, b) is None


def test_new_stamp_with_old_receipt_is_rejected():
    a = snapshot(10, scene())
    b = replace(snapshot(13, scene()), received_monotonic=(19.0, 20.0, 20.0))
    assert verify(a, b) is None


def test_compact_envelope_rejects_front_face_large_target_and_missing_geometry():
    from types import SimpleNamespace as NS

    def surface(u, v):
        return (0.3 + (u - 200) * 0.0001, (v - 150) * 0.0001, 0.04)

    observation = NS(image_size=(400, 300), surface_point_in_base=surface)
    assert compact_upper_surface(observation, BOX, POINT) == pytest.approx((0.3, 0, 0.04))
    observation.surface_point_in_base = lambda u, v: (0.3 + (u - 200) * 0.002, (v - 150) * 0.0001, 0.04)
    assert compact_upper_surface(observation, BOX, POINT) is None
    observation.surface_point_in_base = lambda u, v: (0.3, 0, 0.02 if (u, v) == (200, 150) else 0.04)
    assert compact_upper_surface(observation, BOX, POINT) is None
    observation.surface_point_in_base = lambda u, v: None
    assert compact_upper_surface(observation, BOX, POINT) is None


def test_wrist_stationary_verdict_rejects_target_motion_or_new_occlusion():
    def encode(a):
        return cv2.imencode(".jpg", a)[1].tobytes()

    old = encode(scene())
    assert same_wrist_patch(old, encode(scene(exposure=5)), BOX)
    assert not same_wrist_patch(old, encode(scene(shift=15)), BOX)
    image = scene()
    image[100:200, 150:250] = 0
    assert not same_wrist_patch(old, encode(image), BOX)
    assert not same_wrist_patch(old, b"broken", BOX)


def test_aperture_is_center_relative_not_only_total_width():
    from types import SimpleNamespace as NS

    yaw = np.arctan2(0.05285, 0.3 - 0.086)
    jaw = np.array([-np.sin(yaw), np.cos(yaw)])
    other = np.array([np.cos(yaw), np.sin(yaw)])
    center = np.array([0.3, 0.0])

    def observation(offset):
        def surface(u, v):
            xy = center + ((u - 200) / 120 * 0.06) * jaw + ((v - 150) / 120 * 0.01) * other
            if (u, v) == (200, 150):
                xy = center + offset * jaw
            return (*xy, 0.04)

        return NS(image_size=(400, 300), surface_point_in_base=surface)

    # Same 60mm geometry; only the proposed grasp center changes.
    assert compact_upper_surface(observation(0), BOX, POINT) is not None
    assert compact_upper_surface(observation(0.02), BOX, POINT) is None
    assert compact_upper_surface(observation(-0.02), BOX, POINT) is None
