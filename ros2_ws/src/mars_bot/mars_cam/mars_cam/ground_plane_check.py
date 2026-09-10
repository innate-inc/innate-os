#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""On-demand check of where the depth cloud puts the floor.

Run on known-flat ground with a clear few metres ahead. Reports the
reconstructed floor's pitch, roll and height error against the TF-predicted
ground plane, and the fraction of floor points that would be marked as
obstacles at each candidate threshold.

This is a diagnostic, not a node in the fleet — it is never launched with the
stack, it collects a handful of clouds and exits.

Usage:
    ros2 run mars_cam ground_plane_check
    ros2 run mars_cam ground_plane_check --ros-args -p num_clouds:=20 -p roi_x_max:=1.0
"""

import sys
from pathlib import Path

import numpy as np
import rclpy
from rcl_interfaces.srv import GetParameters
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, JointState, PointCloud2
from sensor_msgs_py import point_cloud2
from tf2_ros import Buffer, TransformListener

from mars_cam import camera_mount
from mars_cam.calibration_validation import ErrorStats
from mars_cam.ground_plane import (
    LEAK_THRESHOLDS_M,
    Corridor,
    Intrinsics,
    fit_floor,
    height_error_by_range,
    image_radius,
    leak_fractions,
    mount_correction_for,
    quaternion_matrix,
    transform_to_base,
)

BASE_FRAME = "base_link"


class GroundPlaneCheck(Node):
    """Collects a few depth clouds, transforms them to base_link, reports the floor."""

    def __init__(self) -> None:
        super().__init__("ground_plane_check")

        self.declare_parameter("cloud_topic", "/mars/main_camera/points")
        self.declare_parameter("camera_info_topic", "/mars/main_camera/left/camera_info")
        self.declare_parameter("num_clouds", 10)
        self.declare_parameter("depth_node", "/stereo_depth_estimator")
        self.declare_parameter("data_directory", "/home/jetson1/innate-os/data")
        # Persist the measured correction to the per-robot calibration directory
        # instead of only printing it.
        self.declare_parameter("write", False)
        # Defaults come from Corridor so the two cannot drift apart.
        defaults = Corridor()
        self.declare_parameter("roi_x_min", defaults.x_min)
        self.declare_parameter("roi_x_max", defaults.x_max)
        self.declare_parameter("roi_half_width", defaults.half_width)
        self.declare_parameter("roi_z_min", defaults.z_min)
        self.declare_parameter("roi_z_max", defaults.z_max)

        self.cloud_topic = str(self.get_parameter("cloud_topic").value)
        self.num_clouds = int(self.get_parameter("num_clouds").value)
        self.corridor = Corridor(
            x_min=float(self.get_parameter("roi_x_min").value),
            x_max=float(self.get_parameter("roi_x_max").value),
            half_width=float(self.get_parameter("roi_half_width").value),
            z_min=float(self.get_parameter("roi_z_min").value),
            z_max=float(self.get_parameter("roi_z_max").value),
        )

        self._tf_buffer = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)
        self._collected: list[np.ndarray] = []
        self._source_frame = ""
        self._done = False
        self._head_deg: float | None = None
        # What the depth node is ALREADY applying. Pitch and roll are baked into
        # the cloud we receive; height is not, so we apply it below — otherwise
        # the two accumulate differently and the recommendation is inconsistent.
        self._current = self._read_current_correction()
        self._intrinsics: Intrinsics | None = None
        self._last_transform: tuple[np.ndarray, np.ndarray] | None = None

        self.create_subscription(PointCloud2, self.cloud_topic, self._cloud_callback, qos_profile_sensor_data)
        self.create_subscription(JointState, "/joint_states", self._joint_callback, 10)
        self.create_subscription(
            CameraInfo,
            str(self.get_parameter("camera_info_topic").value),
            self._camera_info_callback,
            qos_profile_sensor_data,
        )
        self.get_logger().info(f"Collecting {self.num_clouds} clouds from {self.cloud_topic} — keep the robot still")

    def _cloud_callback(self, msg: PointCloud2) -> None:
        if self._done:
            return

        points = _read_xyz(msg)
        if points.size == 0:
            self.get_logger().warn("Empty cloud — is the camera calibrated and publishing depth?")
            return

        transform = self._lookup(msg.header.frame_id)
        if transform is None:
            return

        rotation, translation = transform
        self._last_transform = transform
        self._source_frame = msg.header.frame_id
        in_base = transform_to_base(points, rotation, translation)
        in_base[:, 2] += self._current.height_m
        self._collected.append(in_base)
        self.get_logger().info(f"  [{len(self._collected)}/{self.num_clouds}] {len(points)} points")

        if len(self._collected) >= self.num_clouds:
            self._done = True
            self._report()
            rclpy.shutdown()

    def _read_current_correction(self) -> camera_mount.MountCorrection:
        """Query the depth node for the corrections already in effect."""
        node_name = str(self.get_parameter("depth_node").value)
        client = self.create_client(GetParameters, f"{node_name}/get_parameters")
        names = ["mount_pitch_correction_deg", "mount_roll_correction_deg", "mount_height_correction_m"]
        if not client.wait_for_service(timeout_sec=3.0):
            self.get_logger().warn(f"{node_name} not reachable — assuming zero correction is applied")
            return camera_mount.MountCorrection()
        future = client.call_async(GetParameters.Request(names=names))
        rclpy.spin_until_future_complete(self, future, timeout_sec=3.0)
        result = future.result()
        if result is None or len(result.values) != len(names):
            self.get_logger().warn("Could not read current mount correction — assuming zero")
            return camera_mount.MountCorrection()
        return camera_mount.MountCorrection(
            pitch_deg=result.values[0].double_value,
            roll_deg=result.values[1].double_value,
            height_m=result.values[2].double_value,
        )

    def _joint_callback(self, msg: JointState) -> None:
        if "joint_head" in msg.name:
            self._head_deg = float(np.degrees(msg.position[msg.name.index("joint_head")]))

    def _camera_info_callback(self, msg: CameraInfo) -> None:
        # p[] is the rectified projection, which is what the cloud was built from.
        self._intrinsics = Intrinsics(
            fx=msg.p[0], fy=msg.p[5], cx=msg.p[2], cy=msg.p[6], width=msg.width, height=msg.height
        )

    def _lookup(self, source_frame: str) -> tuple[np.ndarray, np.ndarray] | None:
        try:
            tf = self._tf_buffer.lookup_transform(BASE_FRAME, source_frame, rclpy.time.Time())
        except Exception as e:  # noqa: BLE001 — tf2 raises several unrelated types
            self.get_logger().warn(f"No transform {BASE_FRAME} <- {source_frame} yet: {e}")
            return None
        q = tf.transform.rotation
        t = tf.transform.translation
        return quaternion_matrix(q.x, q.y, q.z, q.w), np.array([t.x, t.y, t.z])

    def _report(self) -> None:
        points = np.vstack(self._collected)
        fit = fit_floor(points)

        out = self.get_logger().info
        out("=" * 68)
        out(f"GROUND PLANE CHECK — {len(points)} points from {len(self._collected)} clouds")
        out(f"  cloud frame {self._source_frame!r} -> {BASE_FRAME}")
        head = f"{self._head_deg:+.2f} deg" if self._head_deg is not None else "UNKNOWN (is mars_arm up?)"
        out(f"  joint_head {head}")
        out("=" * 68)

        if fit is None:
            self.get_logger().error("Not enough near-floor points to fit a plane. Is the floor in view?")
            return

        out("")
        out(f"Floor plane fit ({fit.num_points} points within the floor band)")
        out(f"  pitch          {fit.pitch_deg:+.3f} deg   (positive = floor rises with distance)")
        out(f"  roll           {fit.roll_deg:+.3f} deg")
        out(f"  height at base {fit.offset_m * 1000:+.1f} mm   (0 = correct)")
        out(f"  residual RMS   {fit.residual.rms:.1f} mm, p95 {fit.residual.p95:.1f} mm")
        out(f"  implied floor height at 1.0m: {fit.height_at(1.0) * 1000:+.1f} mm")

        out("")
        out("Floor height by range (mm)")
        out(f"  {'range (m)':<14}{'n':>8}{'mean':>9}{'median':>9}{'p95':>9}{'max':>9}")
        for low, high, stats in height_error_by_range(points):
            if stats.count == 0:
                continue
            out(
                f"  {f'{low:.1f}-{high:.1f}':<14}{stats.count:>8}{stats.mean:>9.1f}"
                f"{stats.median:>9.1f}{stats.p95:>9.1f}{stats.maximum:>9.1f}"
            )

        out("")
        out("Floor points that would be MARKED as obstacles")
        for threshold, fraction in leak_fractions(points).items():
            out(f"  above {threshold * 1000:>3.0f} mm : {fraction * 100:6.2f}%")

        self._report_stability()
        self._report_corridor(points)
        out("=" * 68)

    def _report_stability(self) -> None:
        """Per-cloud spread of the floor fit — the signature of camera shake.

        The pooled fit averages vibration away, but STVL latches a mark for
        voxel_decay seconds, so a single bad frame outlives the average. What
        matters for phantom obstacles is the worst excursion, not the mean.
        """
        fits = [fit_floor(cloud) for cloud in self._collected]
        pitches = np.array([f.pitch_deg for f in fits if f is not None])
        offsets = np.array([f.offset_m * 1000.0 for f in fits if f is not None])
        if pitches.size < 2:
            return

        out = self.get_logger().info
        swing = float(pitches.max() - pitches.min())
        out("")
        out(f"Frame-to-frame stability across {pitches.size} clouds (vibration shows up here)")
        out(f"  pitch  mean {pitches.mean():+.3f} deg, std {pitches.std():.3f}, peak-to-peak {swing:.3f}")
        out(
            f"  height mean {offsets.mean():+.1f} mm, std {offsets.std():.1f}, "
            f"peak-to-peak {offsets.max() - offsets.min():.1f}"
        )
        # Worst-case frame is what the costmap keeps, so budget against it.
        worst = float(np.abs(pitches - pitches.mean()).max())
        out(
            f"  worst single-frame excursion {worst:.3f} deg -> "
            f"{np.tan(np.radians(worst)) * self.corridor.x_max * 1000:.1f} mm of floor error at "
            f"{self.corridor.x_max:.2f}m"
        )

    def _report_corridor(self, points: np.ndarray) -> None:
        out = self.get_logger().info
        c = self.corridor
        column = points[c.footprint_mask(points)]

        out("")
        out(f"Corridor x {c.x_min}-{c.x_max}m, |y| <= {c.half_width}m — the volume that actually matters")
        if len(column) == 0:
            self.get_logger().warn("  no points in the corridor — is anything in front of the robot?")
            return

        fit = fit_floor(column)
        if fit is not None:
            out(f"  floor pitch    {fit.pitch_deg:+.3f} deg, roll {fit.roll_deg:+.3f} deg")
            out(f"  height at base {fit.offset_m * 1000:+.1f} mm, residual RMS {fit.residual.rms:.1f} mm")
            self._recommend(fit)

        for threshold, fraction in leak_fractions(column, LEAK_THRESHOLDS_M).items():
            marked = int(np.sum(column[:, 2] > threshold))
            out(
                f"  z_min {threshold * 1000:>3.0f} mm would mark {marked:>6} / {len(column)} points ({fraction * 100:5.2f}%)"
            )

        out(f"  points inside the full corridor box: {int(np.sum(c.mask(points)))}")
        self._report_image_position(column)

    def _recommend(self, fit) -> None:
        """Absolute corrections that flatten the floor, from the CORRIDOR fit.

        Deliberately not the global fit: beyond the corridor the floor is far
        enough that depth noise dominates, and those outliers drag a
        least-squares plane badly enough to recommend an over-correction.

        The values printed are ABSOLUTE, not deltas — the measurement already
        has the active correction folded in (pitch and roll via the cloud,
        height applied on read), so what is measured here is the residual.
        """
        pitch_delta, roll_delta = mount_correction_for(fit)
        height_delta = -fit.offset_m
        updated = self._current.plus(pitch_delta, roll_delta, height_delta)

        out = self.get_logger().info
        out("")
        out(
            f"  currently applied: pitch {self._current.pitch_deg:+.3f}  roll {self._current.roll_deg:+.3f}  "
            f"height {self._current.height_m * 1000:+.1f}mm"
        )
        out(
            f"  residual measured: pitch {fit.pitch_deg:+.3f}  roll {fit.roll_deg:+.3f}  "
            f"height {fit.offset_m * 1000:+.1f}mm"
        )
        out("")
        out("  SET THESE (absolute, per-robot):")
        out(f"    mount_pitch_correction_deg: {updated.pitch_deg:.3f}")
        out(f"    mount_roll_correction_deg:  {updated.roll_deg:.3f}")
        out(f"    mount_height_correction_m:  {updated.height_m:.4f}")

        if bool(self.get_parameter("write").value):
            self._persist(updated, fit)
        else:
            out("  (re-run with -p write:=true to save these to the robot's calibration directory)")

    def _persist(self, updated, fit) -> None:
        out = self.get_logger().info
        data_dir = Path(str(self.get_parameter("data_directory").value))
        calib_dir = camera_mount.find_calibration_dir(data_dir)
        if calib_dir is None:
            self.get_logger().error(
                f"No *calibration_config directory under {data_dir} — cannot save. Calibrate the stereo pair first."
            )
            return

        record = camera_mount.MountCorrection(
            pitch_deg=updated.pitch_deg,
            roll_deg=updated.roll_deg,
            height_m=updated.height_m,
            head_angle_deg=self._head_deg if self._head_deg is not None else 0.0,
            residual_rms_mm=fit.residual.rms,
        )
        try:
            path = camera_mount.save(camera_mount.mount_path(calib_dir), record)
        except Exception as e:  # noqa: BLE001 — a diagnostic must not traceback at the operator
            self.get_logger().error(f"Failed to save mount correction: {e}")
            return
        out(f"  saved to {path}")
        out(f"  measured at joint_head {record.head_angle_deg:+.2f} deg — the floor tilt drifts with")
        out("  head angle, so this is exact only near that position")

    def _report_image_position(self, column: np.ndarray) -> None:
        """Where the corridor lands on the sensor — centre is where the lens model fits."""
        if self._intrinsics is None or self._last_transform is None:
            return
        rotation, translation = self._last_transform
        radii = image_radius(column, rotation, translation, self._intrinsics)
        if radii.size == 0:
            return
        stats = ErrorStats.of(radii)
        out = self.get_logger().info
        out("")
        out("Where the corridor lands in the image (0 = centre, 1 = corner)")
        out(f"  r_norm mean {stats.mean:.3f}, median {stats.median:.3f}, p95 {stats.p95:.3f}, max {stats.maximum:.3f}")
        for label, low, high in (("center", 0.0, 1 / 3), ("middle", 1 / 3, 2 / 3), ("outer", 2 / 3, 9.9)):
            share = float(np.mean((radii >= low) & (radii < high)))
            out(f"  {label:<7} {share * 100:5.1f}%")


def _read_xyz(msg: PointCloud2) -> np.ndarray:
    raw = point_cloud2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)
    if raw is None or len(raw) == 0:
        return np.empty((0, 3))
    return np.column_stack([raw["x"], raw["y"], raw["z"]]).astype(np.float64)


def main(args=None):
    rclpy.init(args=args)
    node = GroundPlaneCheck()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        if node._collected:
            node._report()
    except Exception as e:  # noqa: BLE001 — a diagnostic must not traceback at the operator
        if "context is not valid" not in str(e):
            print(f"[ground_plane_check] {e}", file=sys.stderr)
    finally:
        try:
            node.destroy_node()
        except Exception:
            pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
