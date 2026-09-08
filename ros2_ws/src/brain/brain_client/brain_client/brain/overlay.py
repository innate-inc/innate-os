# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drawing the people boxes on the head frame the model is about to see.

PURE module: cv2 and numpy only, no rclpy, no I/O. The tag drawn on a box and
the tag in the People block are the same string, which is the whole point —
the model can say "the person on the left" and mean P4 (docs/rfc/people-memory.md
section 7). A few milliseconds per turn: decode, rectangle, label, re-encode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

from brain_client.people.types import IdentityState

if TYPE_CHECKING:
    from brain_client.people.types import PeopleSnapshotDict, PersonInViewDict

JPEG_QUALITY = 80  # the driver's own quality for /left/image_raw/compressed

_KNOWN_BGR = (166, 184, 20)  # teal: face-confirmed this encounter
_TENTATIVE_BGR = (11, 158, 245)  # amber: unknown, familiar, or a body-only match
_CONFLICT_BGR = (68, 68, 239)  # red: the evidence disagrees, learning frozen
_BOX_THICKNESS = 2
_LABEL_PAD = 3
_FONT = cv2.FONT_HERSHEY_SIMPLEX
_FONT_THICKNESS = 1
_REFERENCE_WIDTH = 1280.0  # font scale is tied to the frame so a native crop reads the same


def draw_people(jpeg: bytes, snapshot: PeopleSnapshotDict) -> bytes | None:
    """The frame with one box and tag per person, re-encoded at quality 80.

    Returns the input unchanged when the snapshot has nobody to draw, and None
    when the JPEG cannot be decoded — the caller then sends the plain frame and
    tells the model the positions are not drawn.
    """
    # A lost track's box is where the tracker believes the person would be, kept
    # so they are re-associated on return; drawing it names an empty patch.
    people = [p for p in (snapshot.get("people") or []) if not p.get("lost") and _box_of(p)]
    if not people:
        return jpeg
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return None
    for person in people:
        _draw_person(frame, person)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        return None
    return encoded.tobytes()


def _draw_person(frame: np.ndarray, person: PersonInViewDict) -> None:
    height, width = frame.shape[:2]
    box = _box_of(person)
    if box is None:
        return
    ymin, xmin, ymax, xmax = box
    top = _clamp(ymin * height / 1000.0, height)
    left = _clamp(xmin * width / 1000.0, width)
    bottom = _clamp(ymax * height / 1000.0, height)
    right = _clamp(xmax * width / 1000.0, width)
    color = _color_of(person)
    cv2.rectangle(frame, (left, top), (right, bottom), color, _BOX_THICKNESS)
    _draw_label(frame, _label_of(person), left, top, color)


def _draw_label(frame: np.ndarray, label: str, left: int, top: int, color: tuple[int, int, int]) -> None:
    height, width = frame.shape[:2]
    scale = max(0.4, width / _REFERENCE_WIDTH)
    (text_w, text_h), baseline = cv2.getTextSize(label, _FONT, scale, _FONT_THICKNESS)
    box_h = text_h + baseline + 2 * _LABEL_PAD
    # Above the box, or inside it when the person reaches the top of the frame.
    label_top = top - box_h if top - box_h >= 0 else min(top, height - box_h)
    label_left = min(left, max(0, width - text_w - 2 * _LABEL_PAD))
    cv2.rectangle(
        frame,
        (label_left, label_top),
        (label_left + text_w + 2 * _LABEL_PAD, label_top + box_h),
        color,
        cv2.FILLED,
    )
    cv2.putText(
        frame,
        label,
        (label_left + _LABEL_PAD, label_top + box_h - baseline - _LABEL_PAD),
        _FONT,
        scale,
        _text_color(color),
        _FONT_THICKNESS,
        cv2.LINE_AA,
    )


def _box_of(person: PersonInViewDict) -> tuple[int, int, int, int] | None:
    """Per-mille [ymin, xmin, ymax, xmax], the person's body box or, failing
    that, their head."""
    box = person.get("bbox") or person.get("head_bbox")
    if not box or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = (int(value) for value in box)
    if ymax <= ymin or xmax <= xmin:
        return None
    return ymin, xmin, ymax, xmax


def _color_of(person: PersonInViewDict) -> tuple[int, int, int]:
    state = person.get("state") or IdentityState.UNKNOWN
    if state == IdentityState.CONFLICT:
        return _CONFLICT_BGR
    return _KNOWN_BGR if state == IdentityState.KNOWN else _TENTATIVE_BGR


def _label_of(person: PersonInViewDict) -> str:
    # cv2's Hershey fonts are ASCII-only, so the RFC's "P3 · Theo" is drawn
    # with a hyphen; every other surface keeps the middle dot.
    tag = person.get("tag") or "P?"
    name = (person.get("name") or "").strip()
    state = person.get("state") or IdentityState.UNKNOWN
    if state == IdentityState.CONFLICT:
        return f"{tag} - unsure"
    if state == IdentityState.KNOWN and name:
        return f"{tag} - {name}"
    if state == IdentityState.POSSIBLE and name:
        return f"{tag} - probably {name}"
    return f"{tag} - {IdentityState.FAMILIAR if state == IdentityState.FAMILIAR else IdentityState.UNKNOWN}"


def _text_color(background: tuple[int, int, int]) -> tuple[int, int, int]:
    blue, green, red = background
    luminance = 0.114 * blue + 0.587 * green + 0.299 * red
    return (0, 0, 0) if luminance > 140 else (255, 255, 255)


def _clamp(value: float, limit: int) -> int:
    return max(0, min(limit - 1, int(round(value))))
