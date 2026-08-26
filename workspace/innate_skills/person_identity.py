# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fast run-scoped person identity from local face and body embeddings.

The gallery is separate from mission facts: this skill knows that two
observations look like the same person, not what that person ordered or what
the agent should do next. Gallery writes are explicit (``remember`` and
``add_view``); ``identify`` never silently enrolls an uncertain observation.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from innate import Head, HeadState, MainImage, Pose, Skill, SkillOutput, SkillReturn
from innate_skills.mission_run import active_run_id, write_artifact_set
from innate_skills.person_identity_embeddings import (
    BODY_PLAUSIBLE_THRESHOLD,
    FACE_PLAUSIBLE_THRESHOLD,
    identity_decision,
    profile_scores,
    serialized_embedding,
    shared_encoder,
)

STATE_VERSION = 1
MAX_VIEWS_PER_PERSON = 4
IDENTIFY_FRAME_TIMEOUT_S = 5.0
PERSON_VIEW_HEAD_PITCH_DEG = 20.0
HEAD_POSITION_TOLERANCE_DEG = 2.0
HEAD_SETTLE_TIMEOUT_S = 4.0
OBSERVATION_TTL_S = 90.0
OBSERVATION_MAX_TRANSLATION_M = 0.35
OBSERVATION_MAX_ROTATION_RAD = math.radians(30.0)
ARTIFACT_RELATIVE_DIR = Path("workspace/skill_storage/household_orders")


def _result(
    code: str, payload: dict[str, Any] | None = None, *, image: bytes | None = None
) -> SkillOutput:
    data = payload or {}
    return SkillOutput(
        f"{code} {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}",
        data=data,
        image=image,
    )


def _pose_record(pose: Pose | None) -> dict[str, float] | None:
    if pose is None or not all(
        math.isfinite(value) for value in (pose.x, pose.y, pose.theta)
    ):
        return None
    return {"x": float(pose.x), "y": float(pose.y), "theta": float(pose.theta)}


def _observation_is_current(
    observation: dict[str, Any] | None, pose: Pose | None
) -> tuple[bool, str]:
    if not isinstance(observation, dict):
        return False, "no_pending_observation"
    captured_at = observation.get("captured_at")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        return False, "invalid_observation_time"
    age = time.time() - float(captured_at)
    if not math.isfinite(age) or not -5.0 <= age <= OBSERVATION_TTL_S:
        return False, "observation_expired"
    previous = observation.get("pose")
    current = _pose_record(pose)
    if previous is None or current is None:
        return True, ""
    translation = math.hypot(current["x"] - previous["x"], current["y"] - previous["y"])
    rotation = abs(
        math.atan2(
            math.sin(current["theta"] - previous["theta"]),
            math.cos(current["theta"] - previous["theta"]),
        )
    )
    if (
        translation > OBSERVATION_MAX_TRANSLATION_M
        or rotation > OBSERVATION_MAX_ROTATION_RAD
    ):
        return False, "robot_moved_since_identify"
    return True, ""


class PersonIdentity(Skill):
    """Recognize people locally and maintain an explicit run-scoped gallery.

    ``identify`` captures evidence but does not store it. ``remember`` creates
    a profile from that observation; ``add_view`` adds it to an existing
    profile. Mission facts and behavior stay outside this skill.
    """

    image: MainImage | None
    pose: Pose | None
    head: Head
    head_position: HeadState | None

    def _fresh_upward_frame(self) -> tuple[MainImage | None, str | None]:
        """Capture identity evidence after the low-mounted camera looks up."""
        current_head = self.wait_for(
            lambda: self.head_position, timeout=HEAD_SETTLE_TIMEOUT_S
        )
        if current_head is None:
            return None, "no_current_head_position"
        target = PERSON_VIEW_HEAD_PITCH_DEG
        if current_head.max_degrees is not None and math.isfinite(
            current_head.max_degrees
        ):
            target = min(target, current_head.max_degrees)
        command = round(target)
        feedback_before_command = current_head.raw_source
        self.head.set_position(command)
        settled = self.wait_for(
            lambda: (
                self.head_position
                if self.head_position is not None
                and (
                    feedback_before_command is None
                    or self.head_position.raw_source is not feedback_before_command
                )
                and abs(self.head_position.pitch_degrees - command)
                <= HEAD_POSITION_TOLERANCE_DEG
                else None
            ),
            timeout=HEAD_SETTLE_TIMEOUT_S,
        )
        if settled is None:
            return None, "head_did_not_settle_upward"
        settled_frame = self.image
        first_frame = self.wait_for(
            lambda: (
                self.image
                if self.image is not None and self.image is not settled_frame
                else None
            ),
            timeout=IDENTIFY_FRAME_TIMEOUT_S,
        )
        if first_frame is None:
            return None, "no_fresh_upward_camera_frame"
        second_frame = self.wait_for(
            lambda: (
                self.image
                if self.image is not None and self.image is not first_frame
                else None
            ),
            timeout=IDENTIFY_FRAME_TIMEOUT_S,
        )
        if second_frame is None:
            return None, "no_second_post_settle_camera_frame"
        return second_frame, None

    @staticmethod
    def _fresh_gallery(run_id: str | None = None) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "run_id": run_id,
            "next_sequence": 1,
            "profiles": [],
            "pending_observation": None,
        }

    def _load_gallery(self) -> dict[str, Any]:
        raw = self.storage.get("gallery")
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return self._fresh_gallery()
        state = self._fresh_gallery(
            raw.get("run_id") if isinstance(raw.get("run_id"), str) else None
        )
        sequence = raw.get("next_sequence")
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > 0
        ):
            state["next_sequence"] = sequence
        used: set[str] = set()
        for raw_profile in raw.get("profiles", []):
            if not isinstance(raw_profile, dict):
                continue
            encounter_id = raw_profile.get("encounter_id")
            if (
                not isinstance(encounter_id, str)
                or not encounter_id
                or encounter_id in used
            ):
                continue
            views = []
            for raw_view in raw_profile.get("views", [])[-MAX_VIEWS_PER_PERSON:]:
                if not isinstance(raw_view, dict):
                    continue
                body, face = (
                    serialized_embedding(raw_view.get("body")),
                    serialized_embedding(raw_view.get("face")),
                )
                image = raw_view.get("image_b64")
                if (
                    (body is not None or face is not None)
                    and isinstance(image, str)
                    and image
                ):
                    views.append({"image_b64": image, "body": body, "face": face})
            if views:
                state["profiles"].append({"encounter_id": encounter_id, "views": views})
                used.add(encounter_id)
        observation = raw.get("pending_observation")
        if isinstance(observation, dict):
            state["pending_observation"] = observation
        return state

    def _save_gallery(self, state: dict[str, Any]) -> None:
        self.storage["gallery"] = state
        self._write_visualization(state)

    @staticmethod
    def _public_data(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": state.get("run_id"),
            "people": [
                {
                    "encounter_id": profile["encounter_id"],
                    "saved_views": len(profile.get("views", [])),
                }
                for profile in state["profiles"]
            ],
            "pending_observation": state.get("pending_observation") is not None,
        }

    def _write_visualization(self, state: dict[str, Any]) -> bytes | None:
        try:
            import base64
            import io

            from PIL import Image as PilImage
            from PIL import ImageDraw

            profiles = state["profiles"]
            width, tile_height = 640, 150
            canvas = PilImage.new(
                "RGB", (width, max(110, 30 + tile_height * len(profiles))), "#111318"
            )
            draw = ImageDraw.Draw(canvas)
            draw.text(
                (16, 10),
                f"Person identity gallery - {len(profiles)} profiles",
                fill="#f0f3f7",
            )
            for row, profile in enumerate(profiles):
                y = 30 + row * tile_height
                draw.text((16, y + 8), profile["encounter_id"], fill="#7dffc4")
                for column, view in enumerate(profile.get("views", [])):
                    try:
                        image = PilImage.open(
                            io.BytesIO(base64.b64decode(view["image_b64"]))
                        ).convert("RGB")
                        image.thumbnail((110, 115))
                        x = 145 + column * 120
                        canvas.paste(image, (x, y + 5))
                        channels = "+".join(
                            name for name in ("face", "body") if view.get(name)
                        )
                        draw.text((x, y + 123), channels, fill="#a7adb8")
                    except (OSError, TypeError, ValueError):
                        continue
            png = io.BytesIO()
            canvas.save(png, format="PNG")
            metadata = self._public_data(state)
            write_artifact_set(
                "identity",
                {
                    "person_identity.png": png.getvalue(),
                    "person_identity.json": json.dumps(metadata, indent=2).encode(),
                },
            )
            return png.getvalue()
        except (OSError, TypeError, ValueError) as error:
            self.logger.warning(
                f"[PersonIdentity] could not refresh visualization: {error}"
            )
            return None

    def _begin(self) -> SkillOutput:
        run_id = active_run_id()
        if run_id is None:
            return _result("RUN_UNINITIALIZED")
        # The previous Gemini-backed implementation used this key. It held
        # duplicate base64 frames but is not part of the local gallery schema.
        if "state" in self.storage:
            del self.storage["state"]
        state = self._load_gallery()
        if state.get("run_id") == run_id:
            return _result("IDENTITY_ALREADY_INITIALIZED", self._public_data(state))
        state = self._fresh_gallery(run_id)
        self._save_gallery(state)
        encoder = shared_encoder()
        return _result(
            "IDENTITY_INITIALIZED",
            {**self._public_data(state), "local_encoder": encoder.diagnostics},
        )

    def _identify(self, _encounter_hint: str | None = None) -> SkillOutput:
        state = self._load_gallery()
        if state.get("run_id") != active_run_id() or state.get("run_id") is None:
            return _result("RUN_UNINITIALIZED")
        started = time.monotonic()
        frame, unavailable_reason = self._fresh_upward_frame()
        camera_ms = (time.monotonic() - started) * 1_000.0
        if frame is None:
            state["pending_observation"] = None
            self._save_gallery(state)
            return _result("IDENTITY_UNAVAILABLE", {"reason": unavailable_reason})
        encoder = shared_encoder()
        if not encoder.available:
            return _result(
                "IDENTITY_UNAVAILABLE",
                {
                    "reason": "local_encoder_unavailable",
                    "diagnostics": encoder.diagnostics,
                },
                image=frame.jpeg,
            )
        inference_started = time.monotonic()
        try:
            embeddings = encoder.encode(frame.jpeg)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
            self.logger.warning(f"[PersonIdentity] local encoding failed: {error}")
            return _result(
                "IDENTITY_UNAVAILABLE",
                {"reason": "local_encoding_failed"},
                image=frame.jpeg,
            )
        inference_ms = (time.monotonic() - inference_started) * 1_000.0
        if embeddings.get("body") is None and embeddings.get("face") is None:
            return _result(
                "IDENTITY_UNAVAILABLE",
                {"reason": "no_identity_embedding"},
                image=frame.jpeg,
            )
        observation = {
            "image_b64": str(frame),
            "body": embeddings.get("body"),
            "face": embeddings.get("face"),
            "captured_at": time.time(),
            "pose": _pose_record(self.pose),
        }
        state["pending_observation"] = observation
        scores = profile_scores(state["profiles"], embeddings)
        decision, encounter_id, plausible, evidence = identity_decision(scores)
        self._save_gallery(state)
        timing = {
            "camera_ms": round(camera_ms, 1),
            "embedding_ms": round(inference_ms, 1),
        }
        self.logger.info(
            f"[SkillPhaseTiming] skill=person_identity phase=local_embedding "
            f"duration_ms={inference_ms:.1f} camera_ms={camera_ms:.1f}"
        )
        if decision == "known":
            return _result(
                "KNOWN_PERSON",
                {"encounter_id": encounter_id, "evidence": evidence, "timing": timing},
                image=frame.jpeg,
            )
        if decision == "ambiguous":
            return _result(
                "IDENTITY_AMBIGUOUS",
                {
                    "plausible_encounter_ids": plausible,
                    "evidence": evidence,
                    "timing": timing,
                },
                image=frame.jpeg,
            )
        return _result("UNKNOWN_PERSON", {"timing": timing}, image=frame.jpeg)

    def _pending(
        self, state: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, SkillOutput | None]:
        observation = state.get("pending_observation")
        current, reason = _observation_is_current(observation, self.pose)
        if not current:
            state["pending_observation"] = None
            self._save_gallery(state)
            return None, _result("IDENTITY_NOT_STORED", {"reason": reason})
        return observation, None

    def _remember(self) -> SkillOutput:
        state = self._load_gallery()
        observation, error = self._pending(state)
        if error is not None:
            return error
        sequence = state["next_sequence"]
        used = {profile["encounter_id"] for profile in state["profiles"]}
        while f"resident-{sequence:03d}" in used:
            sequence += 1
        encounter_id = f"resident-{sequence:03d}"
        state["next_sequence"] = sequence + 1
        view = {key: observation.get(key) for key in ("image_b64", "body", "face")}
        state["profiles"].append({"encounter_id": encounter_id, "views": [view]})
        state["pending_observation"] = None
        self._save_gallery(state)
        return _result(
            "PERSON_REMEMBERED",
            {
                "encounter_id": encounter_id,
                "saved_views": 1,
                "channels": [key for key in ("face", "body") if view.get(key)],
            },
        )

    def _add_view(self, encounter_id: str | None) -> SkillOutput:
        state = self._load_gallery()
        profile = next(
            (
                item
                for item in state["profiles"]
                if item["encounter_id"] == encounter_id
            ),
            None,
        )
        if profile is None:
            return _result("IDENTITY_NOT_STORED", {"reason": "unknown_encounter_id"})
        observation, error = self._pending(state)
        if error is not None:
            return error
        scores = profile_scores([profile], observation)
        only = scores[0]
        face_ok = (
            only.get("face_score") is not None
            and only["face_score"] >= FACE_PLAUSIBLE_THRESHOLD
        )
        body_ok = (
            only.get("body_score") is not None
            and only["body_score"] >= BODY_PLAUSIBLE_THRESHOLD
        )
        if not face_ok and not body_ok:
            return _result(
                "IDENTITY_NOT_STORED",
                {
                    "reason": "view_conflicts_with_profile",
                    "encounter_id": encounter_id,
                    "face_score": only.get("face_score"),
                    "body_score": only.get("body_score"),
                },
            )
        view = {key: observation.get(key) for key in ("image_b64", "body", "face")}
        profile["views"] = [*profile.get("views", []), view][-MAX_VIEWS_PER_PERSON:]
        state["pending_observation"] = None
        self._save_gallery(state)
        return _result(
            "IDENTITY_VIEW_ADDED",
            {"encounter_id": encounter_id, "saved_views": len(profile["views"])},
        )

    def _visualize(self) -> SkillOutput:
        state = self._load_gallery()
        image = self._write_visualization(state)
        return _result(
            "IDENTITY_VISUALIZATION",
            {
                **self._public_data(state),
                "artifact_available": image is not None,
                "image_path": str(ARTIFACT_RELATIVE_DIR / "person_identity.png"),
                "metadata_path": str(ARTIFACT_RELATIVE_DIR / "person_identity.json"),
            },
            image=image,
        )

    def execute(self, action: str, encounter_id: str | None = None) -> SkillReturn:
        normalized = action.strip().lower() if isinstance(action, str) else ""
        if normalized == "begin":
            return self._begin()
        if normalized == "identify":
            return self._identify(encounter_id)
        if normalized == "remember":
            return self._remember()
        if normalized == "add_view":
            return self._add_view(encounter_id)
        if normalized == "visualize":
            return self._visualize()
        self.fail(
            "Unknown action. Use one of: begin, identify, remember, add_view, visualize."
        )
