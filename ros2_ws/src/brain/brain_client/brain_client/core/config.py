# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Typed configuration for the brain client.

PURE module: imports no ``rclpy``. ``BrainConfig.load(node)`` is handed a node so
it can declare/read ROS parameters, but the dataclass itself is plain data —
which keeps every consumer testable without a ROS runtime.

Credentials deliberately stay out of the ROS parameter surface: the brain
reaches its provider through the Innate proxy (INNATE_SERVICE_KEY) or directly
via that vendor's own key (``GEMINI_API_KEY`` / ``OPENAI_API_KEY``), loaded
from ``.env`` by launch.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrainConfig:
    # --- Topics ---
    image_topic: str
    cmd_vel_topic: str
    arm_camera_image_topic: str
    odom_topic: str
    current_nav_mode_topic: str
    current_map_topic: str
    amcl_pose_topic: str
    map_saved_topic: str
    mapping_session_topic: str
    scan_topic: str

    # --- Feature flags ---
    send_arm_camera_image: bool
    log_everything: bool
    simulator_mode: bool

    # --- Camera geometry (for pointed-pixel -> floor-target grounding) ---
    vertical_fov: float  # degrees
    x_cam: float  # camera forward offset from base_link (m)
    height_cam: float  # camera height above the floor (m)

    # --- Local brain ---
    brain_backend: str  # which provider it thinks with: "gemini" | "openai"
    brain_model: str
    brain_thinking_level: str  # written in the provider's vocabulary; "" = model default
    memory_model: str  # spatial memory search is Gemini-only, whatever the brain runs on
    idle_turn_interval: float  # seconds between looks when no skill is running
    supervision_turn_interval: float  # seconds between looks while a skill runs
    history_max_entries: int  # conversation entries kept for the model
    history_max_image_turns: int  # frame-turn floor: compaction keeps 1-2x this many (wrist keeps only the newest)

    # --- Timing ---
    scan_stale_after_sec: float
    timezone: str  # IANA name for the agent's wall clock; "" = the host's local zone

    # --- Proxy service config (credentials come from env, not params) ---
    cartesia_voice_id: str

    @property
    def proxy_config(self) -> dict:
        return {"cartesia_voice_id": self.cartesia_voice_id}

    @classmethod
    def load(cls, node) -> BrainConfig:
        """Declare every parameter on ``node`` and read it into a frozen config.

        Each default's Python type picks the ROS accessor, and ``cls(**...)``
        makes a name that drifts from the dataclass fail loudly instead of
        being silently declared-and-ignored.
        """
        accessor = {str: "string_value", bool: "bool_value", int: "integer_value", float: "double_value"}
        for name, default in _PARAM_DEFAULTS.items():
            node.declare_parameter(name, default)
        values = {
            name: getattr(node.get_parameter(name).get_parameter_value(), accessor[type(default)])
            for name, default in _PARAM_DEFAULTS.items()
        }
        return cls(**_carry_renamed(node, values))


# One default per BrainConfig field, in field order; a value's type must match
# its field's (it selects the ROS parameter accessor in ``load``).
_PARAM_DEFAULTS: dict[str, str | bool | int | float] = {
    # --- Topics ---
    "image_topic": "/mars/main_camera/left/image_raw/compressed",
    "cmd_vel_topic": "/cmd_vel",
    "arm_camera_image_topic": "/mars/arm/image_raw/compressed",
    "odom_topic": "/odom",
    "current_nav_mode_topic": "/nav/current_mode",
    "current_map_topic": "/nav/current_map",
    "amcl_pose_topic": "/amcl_pose",
    "map_saved_topic": "/nav/map_saved",
    "mapping_session_topic": "/nav/mapping_session",
    "scan_topic": "/scan",
    # --- Feature flags ---
    "send_arm_camera_image": True,
    "log_everything": False,
    "simulator_mode": False,
    # --- Camera geometry ---
    "vertical_fov": 80.0,
    "x_cam": 0.0197,
    "height_cam": 0.19663,
    # --- Local brain ---
    "brain_backend": "gemini",
    "brain_model": "gemini-3.6-flash",
    # Each provider names its own levels: gemini takes "minimal" | "low" |
    # "medium" | "high", openai takes "none" | "low" | "medium" | "high" |
    # "xhigh" | "max". "" = the model's default, and an unknown value falls
    # back to that with a warning rather than 400ing every request.
    # Measured on gemini-3.6-flash (2026-08): minimal is ~3x faster than the
    # default level (0.96s vs 3.08s median turn) and passed the same
    # single-turn discipline probes (wait on idle, ignore STT noise,
    # tool choice, go_to_point_in_view grounding). An earlier model's "low"
    # measurably hurt multi-turn instruction-following (skill re-runs,
    # chatter) — if that resurfaces, revert to "" here. The numbers are
    # Gemini's; another provider's levels need their own measurement.
    "brain_thinking_level": "minimal",
    # Spatial memory search needs Gemini context caching and the Files API,
    # which have no counterpart elsewhere, so it stays on Gemini even when the
    # agent thinks with another provider.
    "memory_model": "gemini-3.6-flash",
    "idle_turn_interval": 3.0,
    "supervision_turn_interval": 5.0,
    # Compaction evicts to half the cap, so depth rides 1000-2000 entries. A silent
    # supervision turn stores TWO entries (~50 tokens: status text + masked-frame
    # placeholders + an EMPTY model turn), a tool-call turn three — so 2000 entries
    # is ~700-1000 turns of memory and ~25-50k prompt tokens of text. Masking
    # rewrites frame turns near the tail, so ~94% of each request still hits the
    # cache and full-price spend is flat in this cap — depth costs cached-rate
    # carry, not latency.
    "history_max_entries": 2000,
    "history_max_image_turns": 3,
    # --- Timing ---
    "scan_stale_after_sec": 10.0,
    # The host's own zone by default. Set this when the robot's OS is left on
    # UTC — the agent states the time out loud, so a wrong zone is user-visible.
    "timezone": "",
    # --- Proxy service config ---
    "cartesia_voice_id": "9fdaae0b-f885-4813-b589-3c07cf9d5fea",
}


# Settings named before the brain could run on more than one provider. A robot's
# settings.yaml still carries them, and ignoring one would silently revert that
# robot to the default model.
_RENAMED = {"gemini_model": "brain_model", "gemini_thinking_level": "brain_thinking_level"}


def _carry_renamed(node, values: dict) -> dict:
    """Let a retired parameter still set the one that replaced it, and say so."""
    for old, new in _RENAMED.items():
        node.declare_parameter(old, "")
        legacy = node.get_parameter(old).get_parameter_value().string_value
        if not legacy or values[new] != _PARAM_DEFAULTS[new]:
            continue
        node.get_logger().warn(f"[Brain] '{old}' is now '{new}' — using {legacy!r}; rename it in settings.yaml")
        values[new] = legacy
    return values
