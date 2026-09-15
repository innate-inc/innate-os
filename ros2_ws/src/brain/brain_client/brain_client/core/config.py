# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Typed configuration for the brain client.

PURE module: imports no ``rclpy``. ``BrainConfig.load(node)`` is handed a node so
it can declare/read ROS parameters, but the dataclass itself is plain data —
which keeps every consumer testable without a ROS runtime.

Credentials deliberately stay out of the ROS parameter surface: the brain
reaches its model through the Innate proxy (INNATE_SERVICE_KEY), an
OpenAI-compatible endpoint (``LLM_API_KEY``), or Google directly
(``GEMINI_API_KEY``) — all from the environment (loaded from ``.env`` by launch).
"""

from __future__ import annotations

from dataclasses import dataclass

GEMINI_ROUTE = "gemini"
"""The ``memory_llm_base_url`` value naming the managed Gemini route — the proxy
or ``GEMINI_API_KEY`` — rather than an endpoint of the operator's own."""


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

    # --- Local brain (the model wire) ---
    llm_base_url: str  # OpenAI-compatible ".../v1" root; "" = Gemini (proxy or GEMINI_API_KEY)
    llm_model: str
    llm_thinking: str  # sent as reasoning_effort; "" = server default
    llm_extra_body: str  # JSON object merged into every request
    # --- Memory search (its own wire, or the brain's) ---
    memory_llm_base_url: str  # "" = the brain's wire; "gemini" = the managed Gemini route; else a ".../v1" root
    memory_llm_model: str
    memory_llm_thinking: str
    memory_llm_extra_body: str
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
        for retired in _RENAMED_PARAMS:
            node.declare_parameter(retired, UNSET)
        values = {
            name: getattr(node.get_parameter(name).get_parameter_value(), accessor[type(default)])
            for name, default in _PARAM_DEFAULTS.items()
        }
        for retired, current in _RENAMED_PARAMS.items():
            carried = node.get_parameter(retired).get_parameter_value().string_value.strip()
            if carried in _ABSENT_VALUES[current] or values[current] not in _ABSENT_VALUES[current]:
                continue
            values[current] = carried
            node.get_logger().warn(f"[Brain] '{retired}' is retired — using {carried!r} as '{current}'; rename it")
        if values["llm_model"] in _ABSENT_VALUES["llm_model"]:
            values["llm_model"] = DEFAULT_MODEL
        if values["llm_thinking"] in _ABSENT_VALUES["llm_thinking"]:
            values["llm_thinking"] = "" if values["llm_base_url"].strip() else GEMINI_THINKING
        warning = _resolve_memory_wire(values)
        if warning:
            node.get_logger().warn(warning)
        return cls(**values)


def _resolve_memory_wire(values: dict) -> str | None:
    """Fill the memory search's blank knobs: on the brain's wire they are the
    brain's; on the gemini route they are Gemini's; on a server of the operator's
    own the thinking level and extras are that server's defaults, and the model —
    which no default can name — is the brain's, returned as a warning to log."""
    route = values["memory_llm_base_url"].strip()
    if not route:
        for knob in ("model", "thinking", "extra_body"):
            if values[f"memory_llm_{knob}"] in _ABSENT_VALUES[f"memory_llm_{knob}"]:
                values[f"memory_llm_{knob}"] = values[f"llm_{knob}"]
        return None
    gemini = route == GEMINI_ROUTE
    if values["memory_llm_thinking"] in _ABSENT_VALUES["memory_llm_thinking"]:
        values["memory_llm_thinking"] = GEMINI_THINKING if gemini else ""
    if values["memory_llm_model"] not in _ABSENT_VALUES["memory_llm_model"]:
        return None
    values["memory_llm_model"] = DEFAULT_MODEL if gemini else values["llm_model"]
    if gemini:
        return None
    return (
        f"[Brain] memory_llm_model is blank for memory_llm_base_url={route!r} — asking that server for "
        f"the brain's model {values['llm_model']!r}; set memory_llm_model if it names its model differently"
    )


UNSET = "<unset>"
"""Declared default of every field with a retired alias, so an explicit value can be
told from an absent one — a value-only comparison would let ``GEMINI_MODEL`` from the
environment outrank the robot's own ``gemini_model`` in settings.yaml."""

# Old parameter name -> its replacement, honoured from settings.yaml and from .env
# (GEMINI_MODEL, which the launch feeds to the retired parameter so settings.yaml
# still outranks it); ignoring either would silently revert a robot's model.
_RENAMED_PARAMS = {"gemini_model": "llm_model", "gemini_thinking_level": "llm_thinking"}

# Applied after the carry above, so a retired name still decides the value.
DEFAULT_MODEL = "gemini-3.6-flash"
# Measured on 3.6-flash (2026-08): ~3x faster than the default level at the same
# discipline probes; an earlier model's "low" hurt multi-turn following (revert to ""
# if that resurfaces). Gemini-specific, so a configured endpoint defaults to "".
GEMINI_THINKING = "minimal"

# What counts as "nobody set this". An empty llm_thinking is explicit — it asks for the
# server's own thinking default — while an empty model name never is (the launch passes
# "" when LLM_MODEL is absent from the environment).
_ABSENT_VALUES = {
    "llm_model": (UNSET, ""),
    "llm_thinking": (UNSET,),
    "memory_llm_model": (UNSET, ""),
    "memory_llm_thinking": (UNSET,),
    "memory_llm_extra_body": ("",),
}

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
    # --- Local brain (the model wire) ---
    # Empty = Gemini through the Innate proxy or GEMINI_API_KEY; any other
    # OpenAI-compatible server is its ".../v1" root plus LLM_API_KEY.
    "llm_base_url": "",
    # These two have retired aliases, so their real defaults land in load(), after the carry.
    "llm_model": UNSET,
    "llm_thinking": UNSET,
    # Server-specific request fields, e.g. {"chat_template_kwargs": {"enable_thinking": false}}
    # for Nemotron 3 / Qwen3 under vLLM, or {"google": {...}} on Google's compat layer.
    "llm_extra_body": "",
    # --- Memory search ---
    # Blank = the brain's own wire and knobs. "gemini" keeps recall on Gemini (with its
    # context cache) while the brain runs elsewhere; a ".../v1" root is a server of its
    # own, keyed by MEMORY_LLM_API_KEY. The three knobs below then default like the
    # brain's do on that route — resolved in load(), after the brain's own.
    "memory_llm_base_url": "",
    "memory_llm_model": UNSET,
    "memory_llm_thinking": UNSET,
    "memory_llm_extra_body": "",
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
