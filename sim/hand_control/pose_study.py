"""Local, human-labelled robot-pose/hand-gesture pairs; no learned mapping yet."""

import base64
import hashlib
import json
import math
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import mujoco
import numpy as np

if TYPE_CHECKING:
    from engine import ArmWorld

VERSION = 1


def catalogue(world: "ArmWorld", focus: str = "general") -> list[dict[str, Any]]:
    definitions = [
        ("neutral", "A relaxed starting pose", {}),
        ("pitch_up", "Claw tipped up", {"pitch": -0.5}),
        ("pitch_down", "Claw tipped down", {"pitch": 0.5}),
        ("roll_left", "Claw rolled to one side", {"roll": -0.7}),
        ("roll_right", "Claw rolled to the other side", {"roll": 0.7}),
        ("yaw_left", "Arm turned left", {"yaw": 0.35}),
        ("yaw_right", "Arm turned right", {"yaw": -0.35}),
        ("left", "Claw moved left", {"side": 0.075}),
        ("right", "Claw moved right", {"side": -0.075}),
        ("high", "Arm raised", {"height": 0.19}),
        ("low", "Arm lowered", {"height": 0.07}),
        ("forward", "Arm reaching farther", {"reach": 0.38}),
        ("back", "Arm drawn closer", {"reach": 0.33}),
        ("closed", "Claw closed", {"grip": 0}),
        ("open", "Claw open", {"grip": 1}),
        ("neutral_repeat", "Back to the starting pose", {}),
    ]
    if focus == "floor":
        # Rotation pairs share position/opening; the low open/closed pair then
        # isolates finger closure at a steep tilt. The repeat is held out.
        definitions = [
            ("floor_reference", "Start with a level claw", {"reach": 0.32, "height": 0.07}),
            ("floor_down_mid", "Tip the claw halfway down", {"reach": 0.32, "height": 0.07, "pitch": math.pi / 4}),
            (
                "floor_down_steep",
                "Point the claw toward the floor",
                {"reach": 0.32, "height": 0.07, "pitch": math.radians(80)},
            ),
            (
                "floor_open",
                "Reach down with the claw open",
                {"reach": 0.32, "height": 0.012, "pitch": math.radians(80), "grip": 1},
            ),
            (
                "floor_closed",
                "Close the claw near the floor",
                {"reach": 0.32, "height": 0.012, "pitch": math.radians(80), "grip": 0},
            ),
            (
                "floor_lift",
                "Lift while keeping your grasp",
                {"reach": 0.32, "height": 0.09, "pitch": math.radians(80), "grip": 0},
            ),
            ("floor_repeat", "Return to the level claw", {"reach": 0.32, "height": 0.07}),
        ]
    elif focus == "refine":
        definitions = [
            ("refine_reference", "Start relaxed, with the claw half open", {}),
            ("refine_yaw_left_small", "Claw turned a little left", {"yaw": 0.2}),
            ("refine_yaw_left", "Claw turned farther left", {"yaw": 0.45}),
            ("refine_yaw_right_small", "Claw turned a little right", {"yaw": -0.2}),
            ("refine_yaw_right", "Claw turned farther right", {"yaw": -0.45}),
            ("refine_roll_left", "Claw rolled to one side", {"roll": -0.55}),
            ("refine_roll_right", "Claw rolled to the other side", {"roll": 0.55}),
            ("refine_pitch_mid", "Claw tilted partway down", {"reach": 0.32, "height": 0.07, "pitch": 0.65}),
            (
                "refine_pitch_steep",
                "Claw pointing almost straight down",
                {"reach": 0.32, "height": 0.07, "pitch": math.radians(80)},
            ),
            (
                "refine_down_left",
                "Claw turned left and tilted down",
                {"reach": 0.32, "height": 0.07, "yaw": 0.35, "pitch": 0.9},
            ),
            (
                "refine_down_right",
                "Claw turned right and tilted down",
                {"reach": 0.32, "height": 0.07, "yaw": -0.35, "pitch": 0.9},
            ),
            (
                "refine_floor_open",
                "Claw open, ready to grasp from the floor",
                {"reach": 0.32, "height": 0.012, "pitch": math.radians(80), "grip": 1},
            ),
            (
                "refine_floor_closed",
                "Same floor pose, with the claw closed",
                {"reach": 0.32, "height": 0.012, "pitch": math.radians(80), "grip": 0},
            ),
            (
                "refine_lift",
                "Lift the closed claw from the floor",
                {"reach": 0.32, "height": 0.09, "pitch": math.radians(80), "grip": 0},
            ),
            (
                "refine_down_left_repeat",
                "Return to the leftward, downward grasp",
                {"reach": 0.32, "height": 0.07, "yaw": 0.35, "pitch": 0.9},
            ),
            ("refine_reference_repeat", "Back to your relaxed starting gesture", {}),
        ]
    elif focus != "general":
        raise ValueError("Unknown pose exercise")
    poses = []
    seed = world.command.copy()
    for key, title, values in definitions:
        target = np.array([values.get("reach", 0.35), -0.05285 + values.get("side", 0), values.get("height", 0.13)])
        target = world.swivel(target, values.get("yaw", 0))
        answers = world.wrist_candidates(target, [values.get("pitch", 0)], values.get("roll", 0))
        if not len(answers):
            raise ValueError(f"Study pose is unreachable in this model: {key}")
        angles = min(answers, key=lambda q: np.linalg.norm(q - seed))
        seed = angles
        grip = values.get("grip", 0.55)
        joints = {f"joint{i + 1}": float(v) for i, v in enumerate(angles)}
        joints["joint6"] = float(world.grip_limits[0] + grip * np.ptp(world.grip_limits))
        world.ik_data.qpos[:] = world.data.qpos
        world.ik_data.qpos[world.qadr] = angles
        mujoco.mj_forward(world.model, world.ik_data)
        poses.append(
            {
                "id": key,
                "title": title,
                "angles": angles.tolist(),
                "joints": joints,
                "ee": world.ik_data.xpos[world.ee].tolist(),
                "grip": grip,
                "orientation_matrix": world.ik_data.xmat[world.ee].tolist(),
                "role": "repeat_check" if key.endswith("_repeat") else "fit",
            }
        )
    return poses


class StudyStore:
    def __init__(self, directory: Path | str, poses: Iterable[dict[str, Any]]) -> None:
        self.directory = Path(directory)
        self.poses = {p["id"]: p for p in poses}
        self.signature = hashlib.sha256(json.dumps(poses, sort_keys=True).encode()).hexdigest()

    def session_path(self, session: object) -> Path:
        if not isinstance(session, str) or str(uuid.UUID(session)) != session:
            raise ValueError("Invalid study session")
        return self.directory / session

    def open(self, session: str | None = None) -> dict[str, Any]:
        session = session or str(uuid.uuid4())
        directory = self.session_path(session)
        path = directory / "manifest.json"
        if path.exists():
            manifest = json.loads(path.read_text())
            if manifest["catalogue_hash"] != self.signature:
                raise ValueError("The robot poses changed. Start a new matching session.")
        else:
            directory.mkdir(parents=True, exist_ok=True)
            manifest = {
                "version": VERSION,
                "session": session,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "catalogue_hash": self.signature,
                "poses": list(self.poses.values()),
                "matches": {},
                "purpose": "User-intended hand gestures for shown robot poses; not scores from the old controller.",
            }
            self.write_manifest(directory, manifest)
        return manifest

    @staticmethod
    def write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
        temporary = directory / "manifest.tmp"
        temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False))
        temporary.replace(directory / "manifest.json")

    def save(self, message: dict[str, Any]) -> dict[str, Any]:
        manifest = self.open(message.get("session"))
        directory = self.session_path(manifest["session"])
        pose_id = message.get("pose_id")
        if pose_id not in self.poses:
            raise ValueError("Unknown study pose")
        comfort = message.get("comfort")
        if comfort not in ("comfortable", "awkward", "unmatchable"):
            raise ValueError("Choose how this pose felt")
        notes = message.get("notes", "")
        if not isinstance(notes, str) or len(notes) > 1000:
            raise ValueError("Notes must be under 1000 characters")
        frames = message.get("frames", [])
        video = None
        if comfort != "unmatchable":
            if not isinstance(frames, list) or not 8 <= len(frames) <= 150:
                raise ValueError("Not enough tracked frames. Please record again.")
            for frame in frames:
                if not isinstance(frame, dict) or not all(
                    type(frame.get(k)) in (float, int) and math.isfinite(frame[k])
                    for k in ("captured_at", "video_time", "inference_ms")
                ):
                    raise ValueError("Invalid recording timestamps")
                for key in ("landmarks", "world_landmarks"):
                    points = frame.get(key)
                    if key == "world_landmarks" and points is None:
                        continue
                    if (
                        not isinstance(points, list)
                        or len(points) != 21
                        or any(
                            not isinstance(p, dict)
                            or not all(
                                type(p.get(axis)) in (int, float) and math.isfinite(p[axis]) for axis in ("x", "y", "z")
                            )
                            for p in points
                        )
                    ):
                        raise ValueError("Invalid hand landmarks")
            if message.get("mime") not in ("video/webm", "video/webm;codecs=vp8", "video/webm;codecs=vp9", "video/mp4"):
                raise ValueError("Unsupported camera recording format")
            encoded = message.get("video", "")
            if not isinstance(encoded, str) or len(encoded) > 8_000_000:
                raise ValueError("Recording is too large")
            video = base64.b64decode(encoded, validate=True)
            if not 100 <= len(video) <= 6_000_000:
                raise ValueError("Empty or oversized camera recording")
        else:
            frames = []
        take = f"{pose_id}-{uuid.uuid4().hex[:12]}"
        record = {
            "version": VERSION,
            "session": manifest["session"],
            "pose_id": pose_id,
            "target": self.poses[pose_id],
            "comfort": comfort,
            "notes": notes,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "frames": frames,
            "camera": message.get("camera"),
            "robot_at_capture": message.get("robot_at_capture"),
        }
        if video:
            filename = take + (".mp4" if message["mime"] == "video/mp4" else ".webm")
            (directory / filename).write_bytes(video)
            record.update(video=filename, video_sha256=hashlib.sha256(video).hexdigest(), mime=message["mime"])
        (directory / f"{take}.json").write_text(json.dumps(record, indent=2, allow_nan=False))
        manifest["matches"][pose_id] = {"record": f"{take}.json", "comfort": comfort, "frames": len(frames)}
        self.write_manifest(directory, manifest)
        return {"session": manifest["session"], "matches": manifest["matches"], "path": str(directory / f"{take}.json")}
