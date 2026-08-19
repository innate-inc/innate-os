# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import base64
import io
import json
import math
import re
import textwrap
import time
from pathlib import Path
from typing import Any

from innate_skills.mission_run import active_run_id, write_artifact_set

from innate import Head, HeadState, MainImage, Pose, Skill, SkillOutput, SkillReturn, resource
from innate import gemini as gemlib

EXPECTED_RESIDENTS = 3
STATE_VERSION = 3
PREVIOUS_STATE_VERSION = 2
LEGACY_STATE_VERSION = 1
MATCH_CONFIDENCE = 0.82
NEW_CONFIDENCE = 0.72
MAX_PENDING_OBSERVATIONS = 6
MAX_CONTACT_REFERENCES = 6
MAX_REFERENCE_IMAGES_PER_PERSON = 2
IDENTIFY_FRAME_TIMEOUT_S = 5.0
PERSON_VIEW_HEAD_PITCH_DEG = 20.0
HEAD_POSITION_TOLERANCE_DEG = 2.0
HEAD_SETTLE_TIMEOUT_S = 4.0
IDENTITY_BINDING_TTL_S = 90.0
IDENTITY_BINDING_MAX_TRANSLATION_M = 0.35
IDENTITY_BINDING_MAX_ROTATION_RAD = math.radians(30.0)
CONTINUITY_HINT_MAX_TRANSLATION_M = 1.0
CONTINUITY_HINT_MAX_ROTATION_RAD = math.radians(45.0)
VALID_STATUSES = {"seen", "asked", "confirmed"}
STATUS_RANK = {"seen": 0, "asked": 1, "confirmed": 2}
VALID_VIEW_QUALITIES = {"good", "limited", "unusable"}
ARTIFACT_RELATIVE_DIR = Path("workspace/skill_storage/household_orders")


def _result(code: str, payload: dict[str, Any] | None = None, *, image: bytes | None = None) -> SkillOutput:
    """Return the small status contract exposed to the agent."""
    data = payload or {}
    message = f"{code} {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}"
    return SkillOutput(message, data=data, image=image)


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _same_name(left: str | None, right: str | None) -> bool:
    return bool(left and right and left.casefold() == right.casefold())


def _clear_identity_binding(state: dict[str, Any]) -> None:
    state["active_encounter_id"] = None
    state["active_observation_image_b64"] = None
    state["active_identified_at"] = None
    state["active_identified_pose"] = None
    state["active_observation_pending_reference"] = False
    state["active_continuity_id"] = None
    state["active_identity_metadata"] = None


def _pose_record(pose: Pose | None) -> dict[str, float] | None:
    if pose is None:
        return None
    values = (pose.x, pose.y, pose.theta)
    if not all(math.isfinite(value) for value in values):
        return None
    return {"x": float(pose.x), "y": float(pose.y), "theta": float(pose.theta)}


def _load_pose_record(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    values = (raw.get("x"), raw.get("y"), raw.get("theta"))
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    x, y, theta = (float(value) for value in values)
    if not all(math.isfinite(value) for value in (x, y, theta)):
        return None
    return {"x": x, "y": y, "theta": math.atan2(math.sin(theta), math.cos(theta))}


def _identity_motion_error(state: dict[str, Any], current_pose: Pose | None) -> tuple[str, str] | None:
    identified_pose = _load_pose_record(state.get("active_identified_pose"))
    if identified_pose is None:
        return None
    if current_pose is None:
        return "current_pose_unavailable", ""
    translation = math.hypot(current_pose.x - identified_pose["x"], current_pose.y - identified_pose["y"])
    rotation = abs(
        math.atan2(
            math.sin(current_pose.theta - identified_pose["theta"]),
            math.cos(current_pose.theta - identified_pose["theta"]),
        )
    )
    if translation > IDENTITY_BINDING_MAX_TRANSLATION_M or rotation > IDENTITY_BINDING_MAX_ROTATION_RAD:
        return (
            "robot_moved_since_identify",
            f"translation_m={translation:.2f} rotation_deg={math.degrees(rotation):.1f}",
        )
    return None


def _continuity_hint_is_current(
    state: dict[str, Any], encounter_id: str, current_pose: Pose | None, *, now: float | None = None
) -> bool:
    """Accept a hint only while it still describes one local physical approach."""
    if not encounter_id or state.get("active_encounter_id") != encounter_id:
        return False

    identified_at = state.get("active_identified_at")
    if isinstance(identified_at, bool) or not isinstance(identified_at, (int, float)):
        return False
    checked_at = time.time() if now is None else now
    binding_age = checked_at - float(identified_at)
    if not math.isfinite(binding_age) or not -5.0 <= binding_age <= IDENTITY_BINDING_TTL_S:
        return False

    identified_pose = _load_pose_record(state.get("active_identified_pose"))
    current_pose_record = _pose_record(current_pose)
    if identified_pose is None or current_pose_record is None:
        return False
    translation = math.hypot(
        current_pose_record["x"] - identified_pose["x"],
        current_pose_record["y"] - identified_pose["y"],
    )
    rotation = abs(
        math.atan2(
            math.sin(current_pose_record["theta"] - identified_pose["theta"]),
            math.cos(current_pose_record["theta"] - identified_pose["theta"]),
        )
    )
    return translation <= CONTINUITY_HINT_MAX_TRANSLATION_M and rotation <= CONTINUITY_HINT_MAX_ROTATION_RAD


def _parse_identity_reply(reply: str | None) -> dict[str, Any] | None:
    """Parse the model's small JSON contract without trusting surrounding text."""
    if not isinstance(reply, str):
        return None
    start = reply.find("{")
    end = reply.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(reply[start : end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    decision = _clean_text(parsed.get("decision"), 20).lower()
    if decision not in {"match", "new", "uncertain"}:
        return None

    raw_confidence = parsed.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        return None
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None

    encounter_id = _clean_text(parsed.get("encounter_id"), 80) or None
    view_quality = _clean_text(parsed.get("view_quality"), 20).lower()
    if view_quality not in VALID_VIEW_QUALITIES:
        return None
    reason = _clean_text(parsed.get("reason"), 240)
    has_structured_plausible_ids = "plausible_encounter_ids" in parsed
    raw_plausible_ids = parsed.get("plausible_encounter_ids", [])
    if raw_plausible_ids is None:
        raw_plausible_ids = []
    if not isinstance(raw_plausible_ids, list):
        return None
    plausible_encounter_ids: list[str] = []
    for raw_id in raw_plausible_ids:
        plausible_id = _clean_text(raw_id, 80)
        if plausible_id and plausible_id not in plausible_encounter_ids:
            plausible_encounter_ids.append(plausible_id)
    # Accept the original response contract during a rolling upgrade. Only an
    # old uncertain response that omitted the structured field may recover IDs
    # from prose: an explicit [] is authoritative, and comparative NEW reasons
    # such as "different from resident-001" must not become ambiguous.
    if not has_structured_plausible_ids and decision == "uncertain":
        for plausible_id in re.findall(r"\bresident-\d{3}\b", reason, flags=re.IGNORECASE):
            plausible_id = plausible_id.lower()
            if plausible_id not in plausible_encounter_ids:
                plausible_encounter_ids.append(plausible_id)
    return {
        "decision": decision,
        "encounter_id": encounter_id,
        "plausible_encounter_ids": plausible_encounter_ids,
        "confidence": confidence,
        "view_quality": view_quality,
        "reason": reason,
    }


def _identity_metadata(verdict: dict[str, Any], *, decision: str | None = None) -> dict[str, Any]:
    metadata = {
        "decision": decision or verdict["decision"],
        "confidence": round(float(verdict["confidence"]), 3),
        "view_quality": verdict["view_quality"],
        "reason": _clean_text(verdict.get("reason"), 240),
    }
    plausible_ids = verdict.get("plausible_encounter_ids")
    if isinstance(plausible_ids, list) and plausible_ids:
        metadata["plausible_encounter_ids"] = plausible_ids[:MAX_CONTACT_REFERENCES]
    return metadata


def _load_identity_metadata(entry: dict[str, Any]) -> dict[str, Any] | None:
    raw = entry.get("last_identity")
    if not isinstance(raw, dict):
        return None
    decision = _clean_text(raw.get("decision"), 24).lower()
    view_quality = _clean_text(raw.get("view_quality"), 20).lower()
    confidence = raw.get("confidence")
    if (
        decision not in {"match", "new", "uncertain", "continuity"}
        or view_quality not in VALID_VIEW_QUALITIES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
    ):
        return None
    metadata = {
        "decision": decision,
        "confidence": min(1.0, max(0.0, float(confidence))),
        "view_quality": view_quality,
        "reason": _clean_text(raw.get("reason"), 240),
    }
    raw_plausible_ids = raw.get("plausible_encounter_ids")
    if isinstance(raw_plausible_ids, list):
        plausible_ids = [_clean_text(item, 80) for item in raw_plausible_ids]
        plausible_ids = list(dict.fromkeys(item for item in plausible_ids if item))
        if plausible_ids:
            metadata["plausible_encounter_ids"] = plausible_ids[:MAX_CONTACT_REFERENCES]
    return metadata


def _reference_images(entry: dict[str, Any]) -> list[str]:
    """Read the v2 gallery while accepting one-image v1 roster entries."""
    raw = entry.get("reference_images_b64")
    values = raw if isinstance(raw, list) else [entry.get("reference_image_b64")]
    unique: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in unique:
            unique.append(value)
    if len(unique) <= MAX_REFERENCE_IMAGES_PER_PERSON:
        return unique
    return [unique[0], unique[-1]]


def _references_verified(entry: dict[str, Any]) -> bool:
    return entry.get("reference_images_verified") is not False


def _reference_owner(
    state: dict[str, Any], encoded_image: str, *, excluding: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if not encoded_image:
        return None
    for collection_name in ("encounters", "pending"):
        for entry in state[collection_name]:
            if entry is not excluding and encoded_image in _reference_images(entry):
                return entry
    return None


def _deduplicate_references(state: dict[str, Any]) -> None:
    """Enforce that one exact camera frame can belong to only one profile."""
    owned: set[str] = set()
    for entry in state["encounters"]:
        references = [encoded for encoded in _reference_images(entry) if encoded not in owned]
        entry["reference_images_b64"] = references
        entry.pop("reference_image_b64", None)
        owned.update(references)

    unique_pending: list[dict[str, Any]] = []
    for entry in state["pending"]:
        references = [encoded for encoded in _reference_images(entry) if encoded not in owned]
        if not references:
            continue
        entry["reference_images_b64"] = references
        entry.pop("reference_image_b64", None)
        owned.update(references)
        unique_pending.append(entry)
    state["pending"] = unique_pending


def _add_reference(state: dict[str, Any], entry: dict[str, Any], encoded_image: str) -> bool:
    """Retain the first useful view plus the newest verified alternative."""
    references = _reference_images(entry)
    if not encoded_image:
        entry["reference_images_b64"] = references
        entry.pop("reference_image_b64", None)
        return False
    if _reference_owner(state, encoded_image, excluding=entry) is not None:
        return False
    # Version-1 frames predate the quality gate. The first verified good view
    # replaces that legacy exemplar instead of making a weak crop immortal in
    # gallery slot zero.
    if not _references_verified(entry):
        entry["reference_images_b64"] = [encoded_image]
        entry["reference_images_verified"] = True
        entry.pop("reference_image_b64", None)
        return True
    if encoded_image in references:
        entry["reference_images_b64"] = references
        entry["reference_images_verified"] = True
        entry.pop("reference_image_b64", None)
        return True
    if not references:
        references = [encoded_image]
    elif len(references) == 1:
        references.append(encoded_image)
    else:
        references[-1] = encoded_image
    entry["reference_images_b64"] = references
    entry["reference_images_verified"] = True
    entry.pop("reference_image_b64", None)
    return True


def _merge_references(state: dict[str, Any], target: dict[str, Any], source: dict[str, Any]) -> None:
    if not _references_verified(source):
        if not _reference_images(target):
            target["reference_images_b64"] = _reference_images(source)[:1]
            target["reference_images_verified"] = False
        return
    for encoded_image in _reference_images(source):
        _add_reference(state, target, encoded_image)


def _contact_references(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Choose gallery tiles fairly: one per profile, then alternate views."""
    entries = [*state["encounters"], *state["pending"]]
    galleries = [(entry, _reference_images(entry)) for entry in entries]
    selected: list[tuple[str, str, str]] = []
    for view_index in range(MAX_REFERENCE_IMAGES_PER_PERSON):
        for entry, references in galleries:
            if view_index >= len(references):
                continue
            encounter_id = entry["encounter_id"]
            label = entry.get("name") or ("unresolved" if entry in state["pending"] else "unknown")
            verification = "verified" if _references_verified(entry) else "legacy unverified"
            selected.append(
                (
                    encounter_id,
                    f"{label} {verification} view {view_index + 1}/{len(references)}",
                    references[view_index],
                )
            )
            if len(selected) >= MAX_CONTACT_REFERENCES:
                return selected
    return selected


def _contact_sheet(references: list[tuple[str, str, str]]) -> tuple[str, frozenset[str]] | None:
    """Build one labeled 640x480 JPEG so Gemini sees at most two images."""
    # Pillow is in the simulator image. Keep it lazy so this otherwise useful
    # state skill still loads on a minimal robot image; identify will then take
    # its conservative saved-reference-unavailable path.
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return None

    valid: list[tuple[str, str, Any]] = []
    for encounter_id, label, encoded in references[:MAX_CONTACT_REFERENCES]:
        if not encoded or len(encoded) > 8_000_000:
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(decoded)) as source:
                valid.append((encounter_id, label, source.convert("RGB")))
        except Exception:
            continue

    if not valid:
        return None

    width, height = 640, 480
    margin = 12
    gap = 10
    columns = 2 if len(valid) > 1 else 1
    rows = math.ceil(len(valid) / columns)
    tile_width = (width - 2 * margin - gap * (columns - 1)) // columns
    tile_height = (height - 2 * margin - gap * (rows - 1)) // rows
    label_height = 28
    sheet = Image.new("RGB", (width, height), (20, 24, 30))
    draw = ImageDraw.Draw(sheet)

    for index, (encounter_id, label, source) in enumerate(valid):
        column = index % columns
        row = index // columns
        left = margin + column * (tile_width + gap)
        top = margin + row * (tile_height + gap)
        right = left + tile_width
        bottom = top + tile_height

        draw.rounded_rectangle((left, top, right, bottom), radius=8, fill=(38, 44, 54), outline=(90, 103, 122))
        title = _clean_text(f"{encounter_id} - {label}", 48)
        draw.text((left + 8, top + 7), title, fill=(245, 247, 250))
        available = (max(1, tile_width - 12), max(1, tile_height - label_height - 8))
        fitted = ImageOps.contain(source, available, Image.Resampling.LANCZOS)
        image_left = left + (tile_width - fitted.width) // 2
        image_top = top + label_height + (tile_height - label_height - fitted.height) // 2
        sheet.paste(fitted, (image_left, image_top))

    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=86, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii"), frozenset(item[0] for item in valid)


def _roster_artifact(state: dict[str, Any]) -> bytes | None:
    """Refresh the human-readable roster PNG and return a JPEG for SkillOutput."""
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return None

    rows = [*(entry | {"kind": "resident"} for entry in state["encounters"])]
    rows.extend(entry | {"kind": "unresolved"} for entry in state["pending"])
    width = 1_000
    header_height = 92
    row_height = 212
    margin = 16
    height = header_height + max(1, len(rows)) * row_height + margin
    canvas = Image.new("RGB", (width, height), (17, 20, 26))
    draw = ImageDraw.Draw(canvas)

    seen = len(state["encounters"])
    asked = sum(STATUS_RANK[entry["status"]] >= STATUS_RANK["asked"] for entry in state["encounters"])
    confirmed = sum(entry["status"] == "confirmed" for entry in state["encounters"])
    draw.text((margin, 16), "HOUSEHOLD RESIDENT ROSTER", fill=(245, 247, 250))
    draw.text(
        (margin, 42),
        f"seen {seen}/{EXPECTED_RESIDENTS}   asked {asked}/{EXPECTED_RESIDENTS}   "
        f"confirmed {confirmed}/{EXPECTED_RESIDENTS}   unresolved {len(state['pending'])}",
        fill=(166, 177, 194),
    )
    draw.text(
        (margin, 64),
        "Each tile is a saved VLM reference angle. Repeated tiles under one ID are the same profile.",
        fill=(112, 201, 190),
    )

    if not rows:
        draw.rounded_rectangle(
            (margin, header_height, width - margin, header_height + row_height - margin),
            radius=12,
            fill=(29, 34, 43),
            outline=(67, 76, 91),
        )
        draw.text((margin + 20, header_height + 55), "No residents have been observed yet.", fill=(192, 201, 214))

    for index, entry in enumerate(rows):
        top = header_height + index * row_height
        bottom = top + row_height - 12
        active = entry["encounter_id"] == state.get("active_encounter_id")
        outline = (87, 221, 204) if active else (67, 76, 91)
        draw.rounded_rectangle((margin, top, width - margin, bottom), radius=12, fill=(29, 34, 43), outline=outline)

        references = _reference_images(entry)
        image_left = margin + 12
        for view_index, encoded in enumerate(references):
            tile_left = image_left + view_index * 178
            tile_box = (tile_left, top + 32, tile_left + 166, top + 160)
            try:
                decoded = base64.b64decode(encoded, validate=True)
                with Image.open(io.BytesIO(decoded)) as source:
                    fitted = ImageOps.fit(source.convert("RGB"), (166, 128), method=Image.Resampling.LANCZOS)
                canvas.paste(fitted, (tile_left, top + 32))
            except Exception:
                draw.rectangle(tile_box, fill=(44, 50, 61), outline=(91, 101, 117))
                draw.text((tile_left + 36, top + 88), "unreadable", fill=(216, 125, 125))
            draw.text((tile_left + 4, top + 10), f"view {view_index + 1}", fill=(151, 163, 180))

        details_left = margin + 380
        name = entry.get("name") or ("Unresolved sighting" if entry["kind"] == "unresolved" else "Unknown resident")
        status = entry.get("status", "seen")
        status_color = {"seen": (242, 186, 73), "asked": (102, 173, 235), "confirmed": (87, 221, 144)}.get(
            status, (192, 201, 214)
        )
        draw.text((details_left, top + 18), f"{name}  ·  {entry['encounter_id']}", fill=(245, 247, 250))
        draw.text((details_left, top + 44), status.upper(), fill=status_color)
        verification = "verified" if _references_verified(entry) else "legacy unverified"
        draw.text(
            (details_left + 105, top + 44),
            f"{len(references)} saved angle(s) · {verification}",
            fill=(151, 163, 180),
        )
        identity = _load_identity_metadata(entry)
        if identity is not None:
            identity_text = (
                f"VLM {identity['decision']} · {identity['confidence']:.0%} · {identity['view_quality']}"
                + (f" · {identity['reason']}" if identity["reason"] else "")
            )
            draw.text((details_left, top + 70), identity_text[:88], fill=(112, 201, 190))
        else:
            draw.text((details_left, top + 70), "VLM not compared yet", fill=(120, 130, 145))
        order = entry.get("order")
        if order:
            draw.text((details_left, top + 98), "ORDER", fill=(151, 163, 180))
            for line_index, line in enumerate(textwrap.wrap(order, width=72)[:4]):
                draw.text((details_left, top + 120 + line_index * 18), line, fill=(219, 225, 233))
        elif entry["kind"] == "unresolved":
            draw.text(
                (details_left, top + 108), "Needs another view or authoritative dialogue name.", fill=(219, 171, 105)
            )
        else:
            draw.text((details_left, top + 108), "Order not recorded yet.", fill=(151, 163, 180))

    png = io.BytesIO()
    canvas.save(png, format="PNG", optimize=True)
    jpeg = io.BytesIO()
    canvas.save(jpeg, format="JPEG", quality=88, optimize=True)
    public_state = {
        "expected_residents": state["expected_residents"],
        "active_encounter_id": state.get("active_encounter_id"),
        "residents": [
            {
                "encounter_id": entry["encounter_id"],
                "name": entry.get("name"),
                "status": entry["status"],
                "order": entry.get("order"),
                "saved_angles": len(_reference_images(entry)),
                "references_verified": _references_verified(entry),
                "last_identity": _load_identity_metadata(entry),
            }
            for entry in state["encounters"]
        ],
        "unresolved": [
            {
                "encounter_id": entry["encounter_id"],
                "saved_angles": len(_reference_images(entry)),
                "references_verified": _references_verified(entry),
                "last_identity": _load_identity_metadata(entry),
            }
            for entry in state["pending"]
        ],
    }
    write_artifact_set(
        "roster",
        {
            "resident_roster.png": png.getvalue(),
            "resident_roster.json": json.dumps(public_state, indent=2).encode(),
        },
    )
    return jpeg.getvalue()


class ResidentRoster(Skill):
    """Maintain a run-scoped resident identity and order roster.

    ``begin`` initializes state once per active agent run; ``identify``
    classifies the current camera frame; ``record_order`` stores a name and
    order; ``confirm`` marks an order confirmed; ``status`` reports state; and
    ``visualize`` returns artifacts.
    """

    image: MainImage | None
    pose: Pose | None
    head: Head
    head_position: HeadState | None

    @resource
    def _gemini_client(self):
        return gemlib.make_client()

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "run_id": None,
            "expected_residents": EXPECTED_RESIDENTS,
            "next_sequence": 1,
            "active_encounter_id": None,
            "active_observation_image_b64": None,
            "active_identified_at": None,
            "active_identified_pose": None,
            "active_observation_pending_reference": False,
            "active_continuity_id": None,
            "active_identity_metadata": None,
            "encounters": [],
            "pending": [],
        }

    def _load_state(self) -> dict[str, Any]:
        raw = self.storage.get("state")
        if not isinstance(raw, dict) or raw.get("version") not in {
            LEGACY_STATE_VERSION,
            PREVIOUS_STATE_VERSION,
            STATE_VERSION,
        }:
            return self._fresh_state()

        state = self._fresh_state()
        raw_version = raw.get("version")
        legacy_state = raw_version == LEGACY_STATE_VERSION
        run_id = raw.get("run_id")
        if isinstance(run_id, str):
            state["run_id"] = run_id
        sequence = raw.get("next_sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
            state["next_sequence"] = sequence

        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for item in raw.get("encounters", []):
            if not isinstance(item, dict):
                continue
            encounter_id = _clean_text(item.get("encounter_id"), 80)
            status = _clean_text(item.get("status"), 20).lower()
            if not encounter_id or encounter_id in seen_ids or status not in VALID_STATUSES:
                continue
            name = _clean_text(item.get("name"), 80) or None
            if name and name.casefold() in seen_names:
                continue
            order = _clean_text(item.get("order"), 1_000) or None
            if status in {"asked", "confirmed"} and (not name or not order):
                status = "seen"
            references = _reference_images(item)
            entry = {
                "encounter_id": encounter_id,
                "name": name,
                "status": status,
                "order": order,
                "reference_images_b64": references,
                "reference_images_verified": (
                    False if legacy_state and references else item.get("reference_images_verified") is not False
                ),
            }
            identity = _load_identity_metadata(item)
            if identity is not None:
                entry["last_identity"] = identity
            state["encounters"].append(entry)
            seen_ids.add(encounter_id)
            if name:
                seen_names.add(name.casefold())

        for item in raw.get("pending", []):
            if not isinstance(item, dict):
                continue
            encounter_id = _clean_text(item.get("encounter_id"), 80)
            references = _reference_images(item)
            if not encounter_id or encounter_id in seen_ids or not references:
                continue
            entry = {
                "encounter_id": encounter_id,
                "status": "seen",
                "reference_images_b64": references,
                "reference_images_verified": (
                    False if legacy_state and references else item.get("reference_images_verified") is not False
                ),
            }
            identity = _load_identity_metadata(item)
            if identity is not None:
                entry["last_identity"] = identity
            state["pending"].append(entry)
            seen_ids.add(encounter_id)

        # Old or interrupted runs may already contain the same JPEG under
        # multiple IDs. Keep the first owner deterministically and remove the
        # duplicate evidence before any new identity decision is made.
        _deduplicate_references(state)
        # v1/v2 stored only a long-lived "active" ID and image, which is the
        # stale-frame path that once assigned Casey's image to Alex. Preserve
        # their roster contents during migration, but never restore that old
        # value as authority for dialogue. Only v3 bindings carry a short wall
        # clock lifetime and an optional robot pose from the identify call.
        if raw_version == STATE_VERSION:
            active_id = _clean_text(raw.get("active_encounter_id"), 80)
            active_entry, _active_collection = self._find_entry(state, active_id)
            active_image = raw.get("active_observation_image_b64")
            pending_reference = raw.get("active_observation_pending_reference") is True
            identified_at = raw.get("active_identified_at")
            raw_identified_pose = raw.get("active_identified_pose")
            identified_pose = _load_pose_record(raw_identified_pose)
            timestamp_valid = (
                not isinstance(identified_at, bool)
                and isinstance(identified_at, (int, float))
                and math.isfinite(float(identified_at))
                and -5.0 <= time.time() - float(identified_at) <= IDENTITY_BINDING_TTL_S
            )
            pose_valid = raw_identified_pose is None or identified_pose is not None
            image_owner = _reference_owner(state, active_image) if isinstance(active_image, str) else None
            image_valid = (
                pending_reference and (image_owner is None or image_owner is active_entry)
            ) or active_image in _reference_images(active_entry or {})
            if (
                active_entry is not None
                and isinstance(active_image, str)
                and active_image
                and image_valid
                and timestamp_valid
                and pose_valid
            ):
                state["active_encounter_id"] = active_id
                state["active_observation_image_b64"] = active_image
                state["active_identified_at"] = float(identified_at)
                state["active_identified_pose"] = identified_pose
                state["active_observation_pending_reference"] = pending_reference
                active_identity = _load_identity_metadata({"last_identity": raw.get("active_identity_metadata")})
                if active_identity is not None:
                    state["active_identity_metadata"] = active_identity
                continuity_id = _clean_text(raw.get("active_continuity_id"), 80)
                continuity, continuity_collection = self._find_entry(state, continuity_id)
                if (
                    continuity is not None
                    and continuity is not active_entry
                    and (
                        continuity_collection == "pending"
                        or (
                            not continuity.get("name")
                            and continuity.get("status") == "seen"
                            and not continuity.get("order")
                        )
                    )
                ):
                    state["active_continuity_id"] = continuity_id
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        _deduplicate_references(state)
        self.storage["state"] = state
        self._refresh_visualization(state)

    def _refresh_visualization(self, state: dict[str, Any]) -> bytes | None:
        try:
            return _roster_artifact(state)
        except Exception as error:
            self.logger.warning(f"[ResidentRoster] could not refresh visualization: {error}")
            return None

    @staticmethod
    def _find_entry(state: dict[str, Any], encounter_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
        if not encounter_id:
            return None, None
        for collection_name in ("encounters", "pending"):
            for entry in state[collection_name]:
                if entry["encounter_id"] == encounter_id:
                    return entry, collection_name
        return None, None

    @staticmethod
    def _find_name(state: dict[str, Any], name: str) -> dict[str, Any] | None:
        for entry in state["encounters"]:
            if _same_name(entry.get("name"), name):
                return entry
        return None

    @staticmethod
    def _allocate_id(state: dict[str, Any]) -> str:
        used = {
            entry["encounter_id"] for collection_name in ("encounters", "pending") for entry in state[collection_name]
        }
        while True:
            sequence = state["next_sequence"]
            state["next_sequence"] = sequence + 1
            encounter_id = f"resident-{sequence:03d}"
            if encounter_id not in used:
                return encounter_id

    def _new_encounter(self, state: dict[str, Any], encoded_image: str) -> dict[str, Any]:
        if _reference_owner(state, encoded_image) is not None:
            raise ValueError("an exact reference image cannot be assigned to a second resident")
        entry = {
            "encounter_id": self._allocate_id(state),
            "name": None,
            "status": "seen",
            "order": None,
            "reference_images_b64": [encoded_image],
            "reference_images_verified": True,
        }
        state["encounters"].append(entry)
        return entry

    def _new_pending(self, state: dict[str, Any], encoded_image: str) -> dict[str, Any]:
        if _reference_owner(state, encoded_image) is not None:
            raise ValueError("an exact reference image cannot be assigned to a second pending profile")
        entry = {
            "encounter_id": self._allocate_id(state),
            "status": "seen",
            "reference_images_b64": [encoded_image],
            "reference_images_verified": True,
        }
        state["pending"].append(entry)
        if len(state["pending"]) > MAX_PENDING_OBSERVATIONS:
            state["pending"] = state["pending"][-MAX_PENDING_OBSERVATIONS:]
        return entry

    @staticmethod
    def _counts(state: dict[str, Any]) -> tuple[int, int, int]:
        encounters = state["encounters"]
        asked = sum(STATUS_RANK[entry["status"]] >= STATUS_RANK["asked"] for entry in encounters)
        confirmed = sum(entry["status"] == "confirmed" for entry in encounters)
        return len(encounters), asked, confirmed

    @staticmethod
    def _public_data(state: dict[str, Any]) -> dict[str, Any]:
        seen, asked, confirmed = ResidentRoster._counts(state)
        residents = [
            {
                "encounter_id": entry["encounter_id"],
                "name": entry.get("name"),
                "status": entry["status"],
                "order": entry.get("order"),
            }
            for entry in state["encounters"]
        ]
        return {
            "expected_residents": state["expected_residents"],
            "seen": seen,
            "asked": asked,
            "confirmed": confirmed,
            "residents": residents,
            "uncertain_observations": len(state["pending"]),
        }

    def _begin(self) -> SkillOutput:
        run_id = active_run_id()
        if run_id is None:
            return _result("RUN_UNINITIALIZED")
        state = self._load_state()
        if state.get("run_id") == run_id:
            return _result("ROSTER_ALREADY_INITIALIZED", {"expected_residents": EXPECTED_RESIDENTS})

        state = self._fresh_state()
        state["run_id"] = run_id
        self._save_state(state)
        return _result("ROSTER_INITIALIZED", {"expected_residents": EXPECTED_RESIDENTS})

    def _identity_result(
        self,
        state: dict[str, Any],
        prefix: str,
        entry: dict[str, Any],
        frame: MainImage,
        identity: dict[str, Any] | None = None,
        *,
        pending_reference: bool = False,
        continuity_id: str | None = None,
    ) -> SkillOutput:
        self.check_cancelled()
        if identity is not None and pending_reference:
            state["active_identity_metadata"] = identity
        else:
            state["active_identity_metadata"] = None
            if identity is not None:
                entry["last_identity"] = identity
        state["active_encounter_id"] = entry["encounter_id"]
        state["active_observation_image_b64"] = str(frame)
        state["active_identified_at"] = time.time()
        state["active_identified_pose"] = _pose_record(self.pose)
        state["active_observation_pending_reference"] = pending_reference
        state["active_continuity_id"] = continuity_id
        self._save_state(state)
        payload = {
            "encounter_id": entry["encounter_id"],
            "resident_status": entry.get("status", "seen"),
        }
        if entry.get("name"):
            payload["name"] = entry["name"]
        if entry.get("order"):
            payload["order"] = entry["order"]
        return _result(prefix, payload, image=frame.jpeg)

    def _fresh_upward_frame(self) -> tuple[MainImage | None, str | None]:
        """Capture identity evidence only after the low camera looks up."""
        current_head = self.wait_for(lambda: self.head_position, timeout=HEAD_SETTLE_TIMEOUT_S)
        if current_head is None:
            return None, "no_current_head_position"

        reported_max = current_head.max_degrees
        target = PERSON_VIEW_HEAD_PITCH_DEG
        if reported_max is not None and math.isfinite(reported_max):
            target = min(target, reported_max)
        command = int(round(target))
        feedback_before_command = current_head.raw_source
        self.head.set_position(command)

        settled = self.wait_for(
            lambda: (
                self.head_position
                if self.head_position is not None
                and (feedback_before_command is None or self.head_position.raw_source is not feedback_before_command)
                and abs(self.head_position.pitch_degrees - command) <= HEAD_POSITION_TOLERANCE_DEG
                else None
            ),
            timeout=HEAD_SETTLE_TIMEOUT_S,
        )
        if settled is None:
            return None, "head_did_not_settle_upward"

        settled_frame = self.image
        first_post_settle_frame = self.wait_for(
            lambda: self.image if self.image is not None and self.image is not settled_frame else None,
            timeout=IDENTIFY_FRAME_TIMEOUT_S,
        )
        if first_post_settle_frame is None:
            return None, "no_fresh_upward_camera_frame"
        frame = self.wait_for(
            lambda: self.image if self.image is not None and self.image is not first_post_settle_frame else None,
            timeout=IDENTIFY_FRAME_TIMEOUT_S,
        )
        if frame is None:
            return None, "no_second_post_settle_camera_frame"
        return frame, None

    def _identify(self, encounter_id: str | None = None) -> SkillOutput:
        state = self._load_state()
        continuity_id = _clean_text(encounter_id, 80)
        continuity, _continuity_collection = self._find_entry(state, continuity_id)
        continuity_is_current = (
            _continuity_hint_is_current(state, continuity_id, self.pose) if continuity is not None else False
        )
        # An identify attempt supersedes any prior dialogue binding. Only a
        # successful _identity_result below is allowed to arm record_order.
        _clear_identity_binding(state)
        self._save_state(state)
        if continuity_id and continuity is None:
            return _result(
                "IDENTITY_UNAVAILABLE",
                {"reason": "unknown_encounter_hint", "encounter_id": continuity_id},
            )
        if continuity_id and not continuity_is_current:
            return _result(
                "IDENTITY_UNAVAILABLE",
                {"reason": "continuity_context_unavailable", "encounter_id": continuity_id},
            )
        # Camera/head feeds are started per root skill run. Identity is
        # independently reframed here, so this is safe after either systematic
        # search or a separate visual approach whose Nav2 tree looked down.
        frame, unavailable_reason = self._fresh_upward_frame()
        if frame is None:
            _clear_identity_binding(state)
            self._save_state(state)
            return _result("IDENTITY_UNAVAILABLE", {"reason": unavailable_reason})

        references = _contact_references(state)
        sheet_result = _contact_sheet(references) if references else None
        if references and sheet_result is None:
            payload = {"reason": "saved_reference_images_unreadable"}
            if continuity is not None:
                payload["encounter_id"] = continuity_id
            return _result(
                "IDENTITY_UNAVAILABLE",
                payload,
                image=frame.jpeg,
            )

        if sheet_result is None:
            sheet = None
            rendered_reference_ids: frozenset[str] = frozenset()
        else:
            sheet, rendered_reference_ids = sheet_result

        if sheet is None:
            prompt = (
                "Assess whether image 1 clearly shows one resident/person, including a stylized simulated avatar, "
                "well enough to save as a future identity reference. A good view does not require the face or head: a "
                "clear continuous view from the legs through most of the torso is sufficient when clothing appearance, "
                "body proportions, and silhouette are distinguishable. A single cropped limb, a view without most of "
                "the torso, a tiny distant person, heavy occlusion, or no person is limited or unusable. A rear or "
                "oblique view can still be good when those stable body-level cues are clear. "
                "Return ONLY one JSON object with this exact shape: "
                '{"decision":"new|uncertain","encounter_id":null,"confidence":0.0,'
                '"view_quality":"good|limited|unusable","plausible_encounter_ids":[],"reason":"short reason"}. '
                "Use new only for one clearly visible person in a good view; otherwise use uncertain."
            )
            images = [str(frame)]
        else:
            prompt = (
                "Image 1 is the person currently in front of the stopped robot. Image 2 is a labeled contact sheet of "
                "earlier encounters. Repeated resident IDs are different saved angles of the same profile. Decide "
                "conservatively whether image 1 is the same physical person as exactly one profile, even when the face "
                "angle, scale, pose, or crop differs. Use multiple stable cues. Face, skin, and hair/head shape are useful "
                "when visible, but they are not required. A clear continuous legs-to-torso view is good when clothing "
                "appearance, body proportions, and silhouette together distinguish the avatar. Clothing color alone must "
                "not decide a match. Classify a view as limited only when it lacks most of the torso, is tiny, or is too "
                "occluded to compare those cues; an oblique view or a crop above the torso is not automatically limited. "
                "Use unusable when no person can be identified. New is allowed only from a good current view with no "
                "plausible matching profile; otherwise choose uncertain. Return ONLY "
                "one JSON object with this exact shape: "
                '{"decision":"match|new|uncertain","encounter_id":"resident-001 or null",'
                '"confidence":0.0,"view_quality":"good|limited|unusable",'
                '"plausible_encounter_ids":["resident-001"],"reason":"short reason"}. '
                "For match, encounter_id must exactly copy a base resident ID visible on the contact sheet. For new use "
                "null. plausible_encounter_ids must list every base resident ID that could reasonably be this person, "
                "and must be [] only when none are plausible. If two or more profiles are plausible, choose uncertain. "
                "Never guess based on roster fullness."
            )
            images = [str(frame), sheet]
        try:
            client = self._gemini_client
            reply = gemlib.ask_image(client, images, prompt, logger=self.logger, retries=2)
        except Exception as error:
            self.logger.warning(f"[ResidentRoster] identity model unavailable: {error}")
            reply = None
        # ask_image may have been inside a successful blocking HTTP request
        # when Stop arrived. Never mutate the roster after that request unless
        # this run is still live.
        self.check_cancelled()

        if reply is None:
            payload = {"reason": "vision_model_unavailable"}
            if continuity is not None:
                payload["encounter_id"] = continuity_id
            return _result(
                "IDENTITY_UNAVAILABLE",
                payload,
                image=frame.jpeg,
            )

        verdict = _parse_identity_reply(reply)
        if verdict is None:
            payload = {"reason": "malformed_identity_response"}
            if continuity is not None:
                payload["encounter_id"] = continuity_id
            return _result(
                "IDENTITY_UNAVAILABLE",
                payload,
                image=frame.jpeg,
            )

        view_quality = verdict["view_quality"]
        # A model-reported confidence score cannot rescue genuinely weak
        # evidence. The prompt deliberately treats a clear legs-through-torso
        # view as good even without the head; limited remains reserved for
        # fragments that lack enough body-level cues to change roster state.
        if view_quality != "good":
            payload = {"reason": "insufficient_identity_view", "view_quality": view_quality}
            if continuity is not None:
                payload["encounter_id"] = continuity_id
            return _result(
                "IDENTITY_UNAVAILABLE",
                payload,
                image=frame.jpeg,
            )

        encoded_frame = str(frame)
        exact_owner = _reference_owner(state, encoded_frame)
        if exact_owner is not None:
            if continuity is not None and continuity is not exact_owner:
                continuity_is_unresolved = _continuity_collection == "pending" or (
                    not continuity.get("name") and continuity.get("status") == "seen" and not continuity.get("order")
                )
                if not continuity_is_unresolved:
                    return _result(
                        "IDENTITY_CONFLICT",
                        {
                            "reason": "continuity_points_to_another_named_profile",
                            "encounter_id": continuity["encounter_id"],
                            "reference_owner": exact_owner["encounter_id"],
                        },
                        image=frame.jpeg,
                    )
                state[_continuity_collection].remove(continuity)
                _merge_references(state, exact_owner, continuity)
            return self._identity_result(
                state,
                "KNOWN_PERSON",
                exact_owner,
                frame,
                identity=_identity_metadata(verdict, decision="match"),
            )

        if not references:
            if continuity is not None:
                return self._identity_result(
                    state,
                    "KNOWN_PERSON",
                    continuity,
                    frame,
                    identity=_identity_metadata(verdict, decision="continuity"),
                    pending_reference=True,
                )
            if verdict["decision"] == "new" and verdict["confidence"] >= NEW_CONFIDENCE:
                entry = self._new_encounter(state, encoded_frame)
                return self._identity_result(
                    state,
                    "NEW_PERSON",
                    entry,
                    frame,
                    identity=_identity_metadata(verdict),
                )
            entry = self._new_pending(state, encoded_frame)
            return self._identity_result(
                state,
                "UNCERTAIN_PERSON",
                entry,
                frame,
                identity=_identity_metadata(verdict, decision="uncertain"),
            )

        decision = verdict["decision"]
        confidence = verdict["confidence"]
        candidate_id = verdict["encounter_id"]
        # Never trust a model-supplied ID unless that ID was successfully
        # decoded and actually rendered into this call's contact sheet.
        candidate, _candidate_collection = (
            self._find_entry(state, candidate_id) if candidate_id in rendered_reference_ids else (None, None)
        )
        plausible_ids: list[str] = []
        for plausible_id in [candidate_id, *verdict["plausible_encounter_ids"]]:
            if plausible_id in rendered_reference_ids and plausible_id not in plausible_ids:
                plausible_ids.append(plausible_id)
        # A fresh physical-continuity hint and a sole plausible visual ID agree
        # on the same resident even if the model conservatively says uncertain.
        # Any other plausible profile still blocks continuity from taking over.
        sole_plausible_continuity = continuity is not None and plausible_ids == [continuity["encounter_id"]]
        if not sole_plausible_continuity and (
            len(plausible_ids) >= 2
            or (plausible_ids and (decision in {"new", "uncertain"} or (decision == "match" and candidate is None)))
        ):
            return _result(
                "AMBIGUOUS_PERSON",
                {"plausible_encounter_ids": plausible_ids},
                image=frame.jpeg,
            )
        if decision == "match" and confidence >= MATCH_CONFIDENCE and candidate is not None:
            if continuity is not None and continuity is not candidate:
                continuity_is_unresolved = _continuity_collection == "pending" or (
                    not continuity.get("name") and continuity.get("status") == "seen" and not continuity.get("order")
                )
                if not continuity_is_unresolved:
                    return self._identity_result(
                        state,
                        "UNCERTAIN_PERSON",
                        continuity,
                        frame,
                        identity=_identity_metadata(verdict, decision="uncertain"),
                        pending_reference=True,
                    )
                # Keep the unresolved continuity profile intact until the
                # resident's spoken name validates this visual match. A false
                # match must be completely reversible.
                deferred_continuity_id = continuity["encounter_id"]
            else:
                deferred_continuity_id = None
            return self._identity_result(
                state,
                "KNOWN_PERSON",
                candidate,
                frame,
                identity=_identity_metadata(verdict, decision="match"),
                pending_reference=True,
                continuity_id=deferred_continuity_id,
            )

        if decision == "new" and confidence >= NEW_CONFIDENCE and continuity is None:
            entry = self._new_encounter(state, encoded_frame)
            return self._identity_result(
                state,
                "NEW_PERSON",
                entry,
                frame,
                identity=_identity_metadata(verdict, decision="new"),
            )

        # A continuity hint represents one uninterrupted physical approach.
        # Only the accepted high-confidence match above may rebind it to some
        # other roster entry; uncertain model output must not silently switch
        # the active resident.
        if continuity is not None:
            candidate = continuity
            pending_reference = True
        else:
            candidate = self._new_pending(state, encoded_frame)
            pending_reference = False
        return self._identity_result(
            state,
            "UNCERTAIN_PERSON",
            candidate,
            frame,
            identity=_identity_metadata(verdict, decision="continuity" if continuity is not None else "uncertain"),
            pending_reference=pending_reference,
        )

    def _record_order(self, name: str | None, order: str | None, encounter_id: str | None) -> SkillOutput:
        raw_state = self.storage.get("state")
        state = self._load_state()
        clean_name = _clean_text(name, 80)
        clean_order = _clean_text(order, 1_000)
        if not clean_name or not clean_order:
            return _result("ORDER_NOT_RECORDED", {"reason": "name_and_full_order_are_required"})

        target_id = _clean_text(encounter_id, 80)
        if not target_id:
            return _result("ORDER_NOT_RECORDED", {"reason": "explicit_encounter_id_required"})
        active_image = state.get("active_observation_image_b64")
        if target_id != state.get("active_encounter_id") or not isinstance(active_image, str) or not active_image:
            raw_identified_at = raw_state.get("active_identified_at") if isinstance(raw_state, dict) else None
            raw_binding_age = (
                time.time() - float(raw_identified_at)
                if not isinstance(raw_identified_at, bool)
                and isinstance(raw_identified_at, (int, float))
                and math.isfinite(float(raw_identified_at))
                else -math.inf
            )
            if (
                isinstance(raw_state, dict)
                and raw_state.get("version") == STATE_VERSION
                and _clean_text(raw_state.get("active_encounter_id"), 80) == target_id
                and raw_binding_age > IDENTITY_BINDING_TTL_S
            ):
                _clear_identity_binding(state)
                self._save_state(state)
                return _result(
                    "ORDER_NOT_RECORDED",
                    {"reason": "identity_context_expired", "encounter_id": target_id},
                )
            return _result(
                "ORDER_NOT_RECORDED",
                {"reason": "encounter_not_currently_identified", "encounter_id": target_id},
            )

        identified_at = state.get("active_identified_at")
        binding_age = (
            time.time() - float(identified_at)
            if not isinstance(identified_at, bool)
            and isinstance(identified_at, (int, float))
            and math.isfinite(float(identified_at))
            else math.inf
        )
        if not -5.0 <= binding_age <= IDENTITY_BINDING_TTL_S:
            _clear_identity_binding(state)
            self._save_state(state)
            return _result(
                "ORDER_NOT_RECORDED",
                {"reason": "identity_context_expired", "encounter_id": target_id},
            )

        motion_error = _identity_motion_error(state, self.pose)
        if motion_error is not None:
            reason, _detail = motion_error
            _clear_identity_binding(state)
            self._save_state(state)
            return _result(
                "ORDER_NOT_RECORDED",
                {"reason": reason, "encounter_id": target_id},
            )
        target, collection_name = self._find_entry(state, target_id)
        if target is None or collection_name is None:
            return _result("ORDER_NOT_RECORDED", {"reason": "identified_encounter_missing"})

        same_name = self._find_name(state, clean_name)

        if target.get("name") and not _same_name(target.get("name"), clean_name):
            # Speech from a nearby/off-camera resident must never be allowed to
            # clone the current visual reference under a new name. Invalidate
            # the binding and require a fresh camera identification.
            identified_name = target["name"]
            _clear_identity_binding(state)
            self._save_state(state)
            return _result(
                "IDENTITY_CONFLICT",
                {
                    "reason": "heard_name_mismatch",
                    "encounter_id": target["encounter_id"],
                    "identified_name": identified_name,
                    "heard_name": clean_name,
                },
            )

        if collection_name == "pending":
            state["pending"].remove(target)
            if same_name is not None:
                _merge_references(state, same_name, target)
                target = same_name
            else:
                target = {
                    "encounter_id": target["encounter_id"],
                    "name": None,
                    "status": "seen",
                    "order": None,
                    "reference_images_b64": _reference_images(target),
                    "reference_images_verified": _references_verified(target),
                }
                state["encounters"].append(target)
        elif same_name is not None and same_name is not target:
            # Dialogue identity is authoritative. Merge an unnamed duplicate
            # into the named encounter instead of creating two roster rows.
            if target.get("name") is None:
                state["encounters"].remove(target)
                _merge_references(state, same_name, target)
            target = same_name

        deferred_continuity_id = _clean_text(state.get("active_continuity_id"), 80)
        deferred_continuity, deferred_collection = self._find_entry(state, deferred_continuity_id)
        if deferred_continuity is not None and deferred_continuity is not target and deferred_collection is not None:
            state[deferred_collection].remove(deferred_continuity)
            _merge_references(state, target, deferred_continuity)

        if state.get("active_observation_pending_reference") is True and not _add_reference(
            state, target, active_image
        ):
            return _result(
                "IDENTITY_CONFLICT",
                {"reason": "current_frame_owned_by_another_profile", "encounter_id": target_id},
            )
        state["active_observation_pending_reference"] = False
        state["active_continuity_id"] = None
        state["active_encounter_id"] = target["encounter_id"]
        active_identity = _load_identity_metadata({"last_identity": state.get("active_identity_metadata")})
        if active_identity is not None:
            target["last_identity"] = active_identity
        state["active_identity_metadata"] = None

        existing_order = target.get("order")
        if target["status"] == "confirmed" and existing_order and existing_order != clean_order:
            self._save_state(state)
            return _result(
                "ORDER_NOT_RECORDED",
                {
                    "reason": "confirmed_order_conflict",
                    "encounter_id": target["encounter_id"],
                    "name": target.get("name"),
                    "existing_order": existing_order,
                },
            )

        target["name"] = clean_name
        target["order"] = clean_order
        if target["status"] != "confirmed":
            target["status"] = "asked"
        self._save_state(state)
        return _result(
            "ORDER_RECORDED",
            {
                "encounter_id": target["encounter_id"],
                "name": clean_name,
                "order": clean_order,
                "resident_status": target["status"],
            },
        )

    def _confirm(self, name: str | None, encounter_id: str | None) -> SkillOutput:
        state = self._load_state()
        clean_name = _clean_text(name, 80)
        if not clean_name:
            return _result("PERSON_NOT_CONFIRMED", {"reason": "explicit_name_required"})
        target_id = _clean_text(encounter_id, 80)
        if not target_id:
            return _result("PERSON_NOT_CONFIRMED", {"reason": "explicit_encounter_id_required"})
        if target_id != state["active_encounter_id"]:
            return _result(
                "PERSON_NOT_CONFIRMED",
                {"reason": "encounter_not_currently_active", "encounter_id": target_id},
            )
        motion_error = _identity_motion_error(state, self.pose)
        if motion_error is not None:
            reason, _detail = motion_error
            _clear_identity_binding(state)
            self._save_state(state)
            return _result(
                "PERSON_NOT_CONFIRMED",
                {"reason": reason, "encounter_id": target_id},
            )
        target, collection_name = self._find_entry(state, target_id)

        if target is None or collection_name != "encounters":
            return _result("PERSON_NOT_CONFIRMED", {"reason": "no_recorded_order_for_encounter"})
        if not _same_name(target.get("name"), clean_name):
            return _result(
                "PERSON_NOT_CONFIRMED",
                {
                    "reason": "name_mismatch",
                    "encounter_id": target["encounter_id"],
                    "recorded_name": target.get("name"),
                    "requested_name": clean_name,
                },
            )
        if target["status"] == "seen" or not target.get("name") or not target.get("order"):
            return _result(
                "PERSON_NOT_CONFIRMED",
                {"reason": "order_not_recorded", "encounter_id": target["encounter_id"]},
            )

        target["status"] = "confirmed"
        _clear_identity_binding(state)
        self._save_state(state)
        return _result(
            "PERSON_CONFIRMED",
            {
                "encounter_id": target["encounter_id"],
                "name": target["name"],
                "order": target["order"],
            },
        )

    def _status(self) -> SkillOutput:
        state = self._load_state()
        self._refresh_visualization(state)
        return _result("ROSTER_STATUS", self._public_data(state))

    def _visualize(self) -> SkillOutput:
        state = self._load_state()
        image = self._refresh_visualization(state)
        return _result(
            "ROSTER_VISUALIZATION",
            {
                "artifact_available": image is not None,
                "image_path": str(ARTIFACT_RELATIVE_DIR / "resident_roster.png"),
                "metadata_path": str(ARTIFACT_RELATIVE_DIR / "resident_roster.json"),
            },
            image=image,
        )

    def execute(
        self,
        action: str,
        name: str | None = None,
        order: str | None = None,
        encounter_id: str | None = None,
    ) -> SkillReturn:
        action = _clean_text(action, 40).lower()
        if action == "begin":
            return self._begin()
        if action == "identify":
            return self._identify(encounter_id)
        if action == "record_order":
            return self._record_order(name, order, encounter_id)
        if action == "confirm":
            return self._confirm(name, encounter_id)
        if action == "status":
            return self._status()
        if action == "visualize":
            return self._visualize()
        self.fail("Unknown action. Use one of: begin, identify, record_order, confirm, status, visualize.")
