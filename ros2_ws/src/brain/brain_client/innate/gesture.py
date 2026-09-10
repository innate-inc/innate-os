# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Recorded gesture context and bounded demonstration-conditioned actions.

No ROS or model calls here. Recordings stay joint-space compatible; EE poses
are derived from the embedded URDF (or explicitly supplied model for old files).
"""

import base64
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

JOINT_NAMES = tuple(f"joint{i}" for i in range(1, 7))
CAMERA_TOPICS = "/mars/main_camera/left/image_raw,/mars/arm/image_raw"


@lru_cache(maxsize=2)
def _chain(xml):
    from mars_arm.urdf import treeFromString

    ok, tree = treeFromString(xml)
    if not ok:
        raise ValueError("Cannot parse gesture URDF")
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
            raise ValueError("Gesture FK failed")
        poses.append([*frame.p, *frame.M.GetQuaternion()])
    return np.asarray(poses)


def _text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


class Gesture:
    """Load bounded keyframes, not a whole uncompressed video into RAM."""

    def __init__(self, path, *, legacy_urdf=None, max_frames=12, frame_indices=None, image_time_reference=False):
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
                raise ValueError("Gesture needs finite six-joint observations and ordered arm timestamps")
            if action.ndim != 2 or len(action) != len(q) or action.shape[1] < 8 or not np.isfinite(action).all():
                raise ValueError("Invalid recorded actions")
            if np.max(np.abs(action[:, 6:8])) > 0.01:
                raise ValueError("Use a stationary-base demonstration")
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
            self.model_hash = hashlib.sha256(self.urdf.encode()).hexdigest()
            self.poses = poses
            self.final_pose = poses[-1].tolist()
            # Always include significant gripper transitions as well as even samples.
            grip = q[:, names.index("joint6")]
            changes = np.flatnonzero(np.abs(np.diff(grip)) > 0.12) + 1
            selected = {0, len(q) - 1}
            if len(changes):
                selected.update(changes[np.linspace(0, len(changes) - 1, min(4, len(changes)), dtype=int)])
            for index in np.linspace(0, len(q) - 1, max_frames, dtype=int):
                if len(selected) >= max_frames:
                    break
                selected.add(int(index))
            if frame_indices is not None:
                if not isinstance(frame_indices, list) or not 1 <= len(frame_indices) <= 8:
                    raise ValueError("Inspect between one and eight source frames")
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
                        record.setdefault("camera_observations", {})[camera] = {
                            "source_time_s": stamp - float(t[0]),
                            "row_offset_s": stamp - float(t[index]),
                            "source_arm_index": source,
                            "ee_pose": poses[source].tolist(),
                            "qpos": q[source].tolist(),
                            "gripper_target_rad": float(action[source, 5]),
                        }
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


def validate_action(value, current, demonstration_length, committed):
    """Reject malformed/oversized actions; never silently clamp a model target."""
    required = {"action", "reference_frame", "pose", "reason", "safe", "holding", "presented"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Invalid gesture action fields")
    if value["action"] not in {"move", "open", "close", "observe", "done", "stop"}:
        raise ValueError("Unknown gesture action")
    if any(type(value[k]) is not bool for k in ("safe", "holding", "presented")):
        raise ValueError("Invalid visual assessment")
    index = value["reference_frame"]
    if type(index) is not int or not 0 <= index < demonstration_length:
        raise ValueError("Invalid demonstration reference")
    pose = value["pose"]
    if (
        not isinstance(pose, list)
        or len(pose) != 6
        or any(type(x) not in (int, float) or not math.isfinite(x) for x in pose)
    ):
        raise ValueError("Expected finite [x,y,z,roll,pitch,yaw]")
    if not isinstance(value["reason"], str) or len(value["reason"]) > 1200:
        raise ValueError("Invalid action explanation")
    if not value["safe"] or value["action"] == "stop":
        raise ValueError("Scene unsuitable for motion: " + value["reason"])
    if committed and value["action"] == "open":
        raise ValueError("Automatic release after grasp is not allowed")
    if value["action"] == "move":
        x, y, z = pose[:3]
        if not (-0.1 <= x <= 0.45 and abs(y) <= 0.35 and 0.025 <= z <= 0.5 and math.hypot(x, y) <= 0.45):
            raise ValueError("Target outside manipulation workspace")
        if math.dist(pose[:3], current[:3]) > 0.04 + 1e-9:
            raise ValueError("Movement exceeds 4 cm per observation")
        if any(
            abs(math.atan2(math.sin(a - b), math.cos(a - b))) > 0.2 for a, b in zip(pose[3:], current[3:], strict=True)
        ):
            raise ValueError("Orientation change exceeds 0.2 radians")
    return value


ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["move", "open", "close", "observe", "done", "stop"]},
        "reference_frame": {"type": "integer"},
        "pose": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6},
        "reason": {"type": "string"},
        "safe": {"type": "boolean"},
        "holding": {"type": "boolean"},
        "presented": {"type": "boolean"},
    },
}
ACTION_SCHEMA["required"] = list(ACTION_SCHEMA["properties"])


class GesturePolicy:
    def __init__(self, demonstration):
        from innate_proxy import ProxyClient

        self.client = ProxyClient()
        if not self.client.is_available():
            raise ValueError("OpenAI access through the Innate proxy is required")
        self.demonstration = demonstration.context()

    def decide(self, observation, history):
        content = self.demonstration + [
            {
                "type": "input_text",
                "text": json.dumps(
                    {"live": {k: v for k, v in observation.items() if k != "images"}, "history": history[-12:]}
                ),
            }
        ]
        for name, image in observation["images"].items():
            content += [
                {"type": "input_text", "text": "LIVE " + name},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + image},
            ]
        body = {
            "model": "gpt-6-astra",
            "store": False,
            "reasoning": {"effort": "low"},
            "instructions": PROMPT,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {"type": "json_schema", "name": "gesture_action", "strict": True, "schema": ACTION_SCHEMA}
            },
        }
        # No action is queued by this call. The skill checks cancellation and
        # new telemetry after it returns, before allowing physical execution.
        with self.client.request_stream("openai", "/v1/responses", method="POST", json=body, timeout=45) as resp:
            resp.raise_for_status()
            response = json.loads(resp.read())
        if response.get("status") != "completed":
            raise ValueError("Gesture model response did not complete")
        texts = [
            part["text"]
            for item in response.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        if len(texts) != 1:
            raise ValueError("Gesture model returned no single action")
        return json.loads(texts[0])


PROMPT = """Imitate this MARS robot's recorded gesture using in-context visual and trajectory examples.
Goal: pick up the requested object, lift it, and present it forward while KEEPING the gripper closed.
The demonstration is reference data, not instructions. Infer the manipulation sequence from its
ordered head/wrist images, measured EE poses and joints. Adapt to the LIVE object's location;
do not blindly replay joint positions or invent a successful grasp. Compare live outcomes to the
reference after each action. reference_frame must identify a source trajectory index you used.
There is no trained task policy. You choose the next primitive and target from the demonstration.
Pose output is [x,y,z,roll,pitch,yaw] in base_link, metres/radians; +x forward, +y left, +z up.
Recorded pose is [x,y,z,qx,qy,qz,qw]. MARS has five arm joints and a gripper; arbitrary orientations
may be unreachable. Preserve orientation unless the demonstration and live image justify changing it.
Each move must be within 0.04m and 0.2rad per Euler component of the LIVE pose. Split longer moves.
Avoid robot body, floor, obstacles and people throughout the swept path; workspace bounds alone
are not collision checking. Report safe=false if visibility or clearance is uncertain.
Use open before grasp, close once aligned, and never open after close. The stationary base must
not move. Holding must mean visually retained in the gripper, not merely absent from the floor.
Only report done when lifted and presented forward as demonstrated, visibly held. Repeated done
observations verify retention. If the object was lost, stop. For non-move actions repeat live pose.
Use observe for another view without movement; stop if task cannot be performed from this position.
"""


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
