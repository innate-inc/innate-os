# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A recorded episode, read as context rather than replayed as a trajectory.

No ROS and no model calls: this only opens a finalized recording, checks it
against the contract the skills rely on, and selects the frames worth sending.
End-effector poses come from the URDF embedded at record time, or from one
supplied explicitly for a recording made before that was stored.
"""

import base64
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

JOINT_NAMES = tuple(f"joint{i}" for i in range(1, 7))
CAMERA_TOPICS = "/mars/main_camera/left/image_raw,/mars/arm/image_raw"


@lru_cache(maxsize=2)
def _chain(xml):
    from mars_arm.urdf import treeFromString

    ok, tree = treeFromString(xml)
    if not ok:
        raise ValueError("Cannot parse the recording's URDF")
    chain = tree.getChain("base_link", "ee_link")
    if not chain.getNrOfJoints():
        raise ValueError("Missing base_link -> ee_link chain")
    return chain


def forward_poses(xml, names, qpos):
    # Reuse the arm's actual FK implementation, including fixed tool transforms.
    import PyKDL as kdl

    qpos = np.asarray(qpos)
    if len(set(names)) != len(names) or qpos.ndim != 2 or qpos.shape[1] != len(names) or not np.isfinite(qpos).all():
        raise ValueError("Invalid measured joints")
    chain = _chain(xml)
    indices = []
    for i in range(chain.getNrOfSegments()):
        joint = chain.getSegment(i).getJoint()
        if joint.getTypeName() != "Fixed":
            indices.append(names.index(joint.getName()))
    solver = kdl.ChainFkSolverPos_recursive(chain)
    poses = []
    for row in qpos:
        q = kdl.JntArray(len(indices))
        for i, source in enumerate(indices):
            q[i] = float(row[source])
        frame = kdl.Frame()
        if solver.JntToCart(q, frame) < 0:
            raise ValueError("Demonstration FK failed")
        poses.append([*frame.p, *frame.M.GetQuaternion()])
    return np.asarray(poses)


def dead_reckon(times, base_action):
    """Integrate recorded /cmd_vel rows into an [x, y, yaw] path in the base's
    starting frame. These are the COMMANDS the base was given, not measured
    odometry — the recorder stores no base pose — so wheel slip and the driver's
    own ramping make this an estimate of where the base went, not a measurement."""
    times = np.asarray(times, dtype=float)
    base_action = np.asarray(base_action, dtype=float)
    # The recorder stores the command in force AT each sample, and it holds
    # forward until the next one replaces it. So a row's velocity covers the
    # interval that follows it, and the path at a row is what the rows before it
    # produced — integrating backwards would offset the base by one sample at
    # every command change, exactly where the model is reading motion.
    span = np.diff(times, append=times[-1])
    forward, turn = base_action[:, 0] * span, base_action[:, 1] * span
    yaw = np.concatenate(([0.0], np.cumsum(turn)[:-1]))
    x = np.concatenate(([0.0], np.cumsum(forward * np.cos(yaw))[:-1]))
    y = np.concatenate(([0.0], np.cumsum(forward * np.sin(yaw))[:-1]))
    return np.column_stack((x, y, yaw))


def grip_events(grip):
    """Indices where the gripper crossed the midpoint of its own travel — one per
    open or close, however long the motion took. A per-sample threshold cannot
    find these: at 30 Hz a real half-second grasp moves only ~0.05 rad per step,
    so a 0.12 test silently never fires on a real recording."""
    grip = np.asarray(grip, dtype=float)
    low, high = float(np.min(grip)), float(np.max(grip))
    if high - low < 0.05:
        return np.empty(0, dtype=int)
    above = grip > 0.5 * (low + high)
    return np.flatnonzero(np.diff(above.astype(np.int8)) != 0) + 1


def model_fingerprint(xml):
    """Identify the robot a URDF describes, not the file it came from. Hashing
    the text rejects a demonstration over a reformat, a comment, or the source
    copy of the same model the installed one was built from."""
    tree = ElementTree.fromstring(xml)
    joints = []
    for joint in sorted(tree.iter("joint"), key=lambda j: j.get("name") or ""):
        origin = joint.find("origin")
        axis = joint.find("axis")
        limit = joint.find("limit")
        joints.append(
            "|".join(
                (
                    joint.get("name") or "",
                    joint.get("type") or "",
                    (origin.get("xyz", "") + ";" + origin.get("rpy", "")) if origin is not None else "",
                    axis.get("xyz", "") if axis is not None else "",
                    (limit.get("lower", "") + ";" + limit.get("upper", "")) if limit is not None else "",
                )
            )
        )
    return hashlib.sha256("\n".join(joints).encode()).hexdigest()


def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


class Demonstration:
    """Load bounded keyframes, not a whole uncompressed video into RAM."""

    def __init__(
        self, path, *, legacy_urdf=None, max_frames=12, frame_indices=None, image_time_reference=False, uniform=False
    ):
        import cv2
        import h5py

        self.path = Path(path).resolve(strict=True)
        if self.path.suffix != ".h5":
            raise ValueError("Select a finalized .h5 episode")
        with h5py.File(self.path, "r") as f:
            q = np.asarray(f["observations/qpos"])
            t = np.asarray(f["timestamps/arm"])
            action = np.asarray(f["action"])
            if (
                q.ndim != 2
                or q.shape[1] != 6
                or not 2 <= len(q) <= 18000
                or not np.isfinite(q).all()
                or t.shape != (len(q),)
                or not np.isfinite(t).all()
                or (t <= 0).any()
                or (np.diff(t) < 0).any()
                or t[-1] <= t[0]
            ):
                raise ValueError("Demonstration needs finite six-joint observations and ordered arm timestamps")
            if action.ndim != 2 or len(action) != len(q) or action.shape[1] < 8 or not np.isfinite(action).all():
                raise ValueError("Invalid recorded actions")
            base_action = action[:, 6:8]
            self.base_moved = bool(np.max(np.abs(base_action)) > 0.01)
            self.base_path = dead_reckon(t, base_action)
            if "observations/ee_pose" in f:
                ds = f["observations/ee_pose"]
                if (
                    _text(ds.attrs.get("frame_id")) != "base_link"
                    or _text(ds.attrs.get("tip_link")) != "ee_link"
                    or _text(ds.attrs.get("columns")) != "x,y,z,qx,qy,qz,qw"
                ):
                    raise ValueError("Unknown EE pose convention")
                if _text(ds.attrs.get("camera_topics", "")) != CAMERA_TOPICS:
                    raise ValueError("Unknown demonstration camera mapping")
                self.urdf = _text(ds.attrs["urdf"])
                names = _text(ds.attrs["joint_names"]).split(",")
                poses = np.asarray(ds)
            else:
                if legacy_urdf is None:
                    raise ValueError("Legacy recording: explicitly supply the URDF used to record it")
                self.urdf = Path(legacy_urdf).read_text()
                names = list(JOINT_NAMES)  # Historical MARS arm/state ordering.
                poses = forward_poses(self.urdf, names, q)
            if len(names) != 6 or set(names) != set(JOINT_NAMES):
                raise ValueError("Unsupported recorded joint names")
            if (
                poses.shape != (len(q), 7)
                or not np.isfinite(poses).all()
                or not np.allclose(np.linalg.norm(poses[:, 3:], axis=1), 1, atol=1e-4)
            ):
                raise ValueError("Invalid EE trajectory")
            self.model_hash = model_fingerprint(self.urdf)
            self.poses = poses
            self.final_pose = poses[-1].tolist()
            # Always include significant gripper transitions as well as even samples.
            # Grasps and releases earn their frames before the even fill: they are
            # where a multi-stage task changes, and an even sample lands between them.
            grip = q[:, names.index("joint6")]
            events = grip_events(grip)
            self.grip_events = [int(i) for i in events]
            # The episode's own aperture scale: a live reading at the closed end
            # means the jaws met with nothing between them.
            self.grip_range = (float(np.min(grip)), float(np.max(grip)))
            changes = np.empty(0, dtype=int) if uniform else events
            selected = {0, len(q) - 1}
            if len(changes):
                keep = min(len(changes), max(4, max_frames // 2))
                selected.update(int(i) for i in changes[np.linspace(0, len(changes) - 1, keep, dtype=int)])
            for index in np.linspace(0, len(q) - 1, max_frames, dtype=int):
                if len(selected) >= max_frames:
                    break
                selected.add(int(index))
            if frame_indices is not None:
                if not isinstance(frame_indices, list) or not 1 <= len(frame_indices) <= 48:
                    raise ValueError("Select between one and forty-eight source frames")
                if any(type(i) is not int or not 0 <= i < len(q) for i in frame_indices):
                    raise ValueError("Invalid source frame index")
                selected = set(frame_indices)
            self.frames = []
            for index in sorted(selected):
                record = {
                    "index": int(index),
                    "time_s": float(t[index] - t[0]),
                    "ee_pose": poses[index].tolist(),
                    "qpos": q[index].tolist(),
                    "gripper_target_rad": float(action[index, 5]),
                    "head_degrees": float(f["head_command"][index]) if "head_command" in f else None,
                    "images": {},
                }
                if self.base_moved:
                    record["base_command"] = base_action[index].tolist()
                    record["base_dead_reckoned"] = self.base_path[index].tolist()
                # Recorder's configured order: head left, wrist. Require both.
                for camera, dataset in (("head", "camera_1"), ("wrist", "camera_2")):
                    images = f[f"observations/images/{dataset}"]
                    stamps = f[f"timestamps/images/{dataset}"]
                    if len(images) != len(q) or len(stamps) != len(q):
                        raise ValueError("Camera/trajectory length mismatch")
                    stamp = float(stamps[index])
                    if not math.isfinite(stamp) or stamp <= 0:
                        raise ValueError("Invalid camera timestamp")
                    if image_time_reference:
                        source = int(np.argmin(np.abs(t - stamp)))
                        if abs(float(t[source]) - stamp) > 0.1:
                            raise ValueError("Image has no nearby recorded arm sample")
                        observed = {
                            "source_time_s": stamp - float(t[0]),
                            "row_offset_s": stamp - float(t[index]),
                            "source_arm_index": source,
                            "ee_pose": poses[source].tolist(),
                            "qpos": q[source].tolist(),
                            "gripper_target_rad": float(action[source, 5]),
                        }
                        if self.base_moved:
                            observed["base_dead_reckoned"] = self.base_path[source].tolist()
                        record.setdefault("camera_observations", {})[camera] = observed
                    elif abs(stamp - t[index]) > 0.25:
                        raise ValueError("Demonstration cameras are not synchronized with the arm")
                    image = np.asarray(images[index])  # Recorder writes BGR, OpenCV expects BGR.
                    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                        raise ValueError("Expected BGR camera frames")
                    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    if not ok:
                        raise ValueError("Cannot encode demonstration image")
                    record["images"][camera] = base64.b64encode(jpeg).decode()
                self.frames.append(record)

    def context(self):
        content = []
        for frame in self.frames:
            content.append(
                {
                    "type": "input_text",
                    "text": "DEMONSTRATION " + json.dumps({k: v for k, v in frame.items() if k != "images"}),
                }
            )
            for name, image in frame["images"].items():
                content.extend(
                    [
                        {"type": "input_text", "text": name},
                        {"type": "input_image", "image_url": "data:image/jpeg;base64," + image},
                    ]
                )
        return content


def convert_recording(source, output, urdf):
    """Copy a legacy episode and add FK observations; never overwrite input/output."""
    import shutil

    import h5py

    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    xml = Path(urdf).read_text()
    with h5py.File(source, "r") as f:
        if "observations/ee_pose" in f:
            raise ValueError("Recording already contains EE poses")
        qpos = np.asarray(f["observations/qpos"])
        if qpos.ndim != 2 or qpos.shape[1] != 6 or not len(qpos) or not np.isfinite(qpos).all():
            raise ValueError("Expected finite six-joint qpos")
        poses = forward_poses(xml, list(JOINT_NAMES), qpos)
    # Exclusive creation protects both original episodes and previous conversions.
    target = output.open("xb")
    try:
        with target, source.open("rb") as original:
            shutil.copyfileobj(original, target)
        with h5py.File(output, "r+") as f:
            ds = f.create_dataset("observations/ee_pose", data=poses)
            ds.attrs.update(
                frame_id="base_link",
                tip_link="ee_link",
                columns="x,y,z,qx,qy,qz,qw",
                source="forward_kinematics_of_observations_qpos",
                urdf=xml,
                joint_names=",".join(JOINT_NAMES),
                camera_topics=CAMERA_TOPICS,
            )
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Add EE poses to a copy of a legacy MARS recording")
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--urdf", required=True, help="Exact URDF used by the recording robot")
    args = parser.parse_args()
    print(convert_recording(args.source, args.output, args.urdf))
