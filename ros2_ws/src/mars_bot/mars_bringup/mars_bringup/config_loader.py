#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Config loader for launch files.

Loads two operator config sources:

* ``.env`` — ``KEY=VALUE`` secrets/URLs pushed into ``os.environ``.
* ``config/settings.yaml`` — a native ROS 2 params file layered last over each
  node's package defaults. Most nodes read it natively via :func:`settings_params`;
  nav2 names limits differently per plugin, so :func:`load_motion_limit_overrides`
  and :func:`load_costmap_rewrites` remap the ``nav`` / ``inflation_layer`` knobs
  onto its schemas.
"""

import os
import sys
from pathlib import Path

import yaml

# Service-key fallback (written by post_update.sh) so INNATE_SERVICE_KEY survives a
# repo reset that loses the innate-os .env. The innate-os .env is loaded last and wins.
SYSTEM_ENV_PATH = Path("/etc/innate.env")


def innate_os_root() -> Path:
    """The innate-os repo root: ``$INNATE_OS_ROOT`` or the ``~/innate-os`` default."""
    return Path(os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")))


def workspace_skills_dir() -> Path:
    """Skill/behavior data dir; passed to nodes whose YAML can't expand ``$INNATE_OS_ROOT``."""
    return innate_os_root() / "workspace" / "custom_skills"


def _load_key_value_env(path: Path) -> None:
    if not path.exists():
        return
    try:
        f = open(path)
    except OSError as e:
        # Transient mount errors (e.g. Docker bind EPERM) shouldn't crash launch; skip.
        print(f"[config_loader] Could not open {path}: {e}", file=sys.stderr)
        return
    with f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ[key] = value


def load_env_file(env_path: Path | None = None) -> None:
    """Load .env into os.environ.

    Reads /etc/innate.env first, then the innate-os .env on top so it wins, while the
    service key still resolves from /etc/innate.env after a repo reset. ``env_path``
    defaults to ``<root>/.env``.
    """
    if env_path is None:
        env_path = innate_os_root() / ".env"

    _load_key_value_env(SYSTEM_ENV_PATH)
    _load_key_value_env(env_path)


def get_env(key: str, default: str = "") -> str:
    """Get an environment variable, or ``default`` if unset."""
    return os.environ.get(key, default)


def _flatten_params(table: dict, prefix: str) -> dict:
    overrides: dict = {}
    for key, value in table.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            overrides.update(_flatten_params(value, full_key))
        else:
            overrides[full_key] = value
    return overrides


def load_yaml_param_defaults(yaml_path) -> dict:
    """Flatten a ROS2 params YAML (node -> ros__parameters -> ...) to dotted names.

    Used to feed package defaults into :func:`load_motion_limit_overrides` so untouched
    limit components are preserved. Returns ``{}`` if the file is unreadable.
    """
    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    defaults: dict = {}
    if isinstance(data, dict):
        for node in data.values():
            params = node.get("ros__parameters") if isinstance(node, dict) else None
            if isinstance(params, dict):
                defaults.update(_flatten_params(params, ""))
    return defaults


def _settings_yaml_path() -> Path:
    return innate_os_root() / "config" / "settings.yaml"


_warned_settings_paths: set[str] = set()


def _warn_settings_unreadable(path: Path, error: Exception) -> None:
    """Warn once per path that settings.yaml couldn't be loaded.

    A malformed file silently reverts every override to package defaults, so surface it
    on stderr instead of vanishing into a bare ``{}``."""
    key = str(path)
    if key in _warned_settings_paths:
        return
    _warned_settings_paths.add(key)
    print(
        f"[config_loader] Failed to load {path}: {error}. "
        "ALL settings.yaml overrides are being IGNORED and package defaults will be used "
        "— fix the YAML to restore your tuning.",
        file=sys.stderr,
    )


# Params ROS declares as doubles. An unquoted ``1`` parses as int and crashes the node with a
# cryptic InvalidParameterTypeException that never names the line, so we reject ints here up front.
# Keep in sync with every float knob in config/settings.yaml.template.
_SETTINGS_DOUBLE_KEYS = frozenset(
    {
        # /**
        "motion_control.max_speed",
        "motion_control.max_angular_speed",
        "motion_control.speed_scale",
        "nav.max_speed",
        "nav.max_angular_speed",
        "inflation_layer.inflation_radius",
        "inflation_layer.cost_scaling_factor",
        # bringup (safety clamp)
        "safety.max_speed",
        "safety.max_angular_speed",
        # mars_arm
        "max_jerk",
        # joystick_controller
        "joystick.slow_mode_factor",
        # mars_app (teleop drive smoothing)
        "motion_control.dt",
        "motion_control.speed_time_constant",
        "motion_control.angular_speed_time_constant",
        "motion_control.max_acceleration",
        "motion_control.max_deceleration",
        "motion_control.max_angular_acceleration",
        "motion_control.max_angular_deceleration",
        "motion_control.max_jerk",
        "motion_control.max_angular_jerk",
        "motion_control.settle_epsilon",
        "motion_control.input_timeout",
        "heading_hold.gain",
        "heading_hold.leak",
        "heading_hold.max_correction",
        "heading_hold.min_speed",
        "heading_hold.straight_yaw",
        "heading_hold.deadband",
        "heading_hold.slew",
        "mad.max_acceleration",
        "mad.max_angular_acceleration",
        # main_camera_driver
        "fps",
        "target_brightness",
        "ae_kp",
        # manipulation_server
        "inference_hz",
        "speed",
        "temporal_ensemble_coeff",
        "replay_base_speed_scale",
        "learned_base_speed_scale",
        # navigation_grid_localizer
        "max_score_threshold",
        "max_range",
        "auto_localize_timeout",
        # brain_client_node
        "vertical_fov",
        "pose_image_interval",
        "scan_stale_after_sec",
        # input_manager_node (barge-in)
        "barge_in_threshold_db",
        "barge_in_reverb_decay",
        # uninavid_node
        "forward_speed",
        "turn_speed",
        "cmd_duration_sec",
        "image_send_hz",
        "cmd_publish_hz",
        "poll_period_sec",
    }
)


def _validate_settings_param_types(data: dict) -> None:
    """Fail fast when a known-double setting is written as a whole number.

    ``bool`` is excluded (it subclasses ``int``)."""
    offenders: list[tuple[str, int]] = []
    for node in data.values():
        params = node.get("ros__parameters") if isinstance(node, dict) else None
        if not isinstance(params, dict):
            continue
        for key, value in _flatten_params(params, "").items():
            if key in _SETTINGS_DOUBLE_KEYS and isinstance(value, int) and not isinstance(value, bool):
                offenders.append((key, value))
    if not offenders:
        return
    details = "\n".join(f"  {key}: {value}  ->  use {value}.0 (a decimal)" for key, value in offenders)
    raise ValueError(
        f"{_settings_yaml_path()}: these values must be decimals, not whole numbers — ROS "
        f"declares them as doubles and rejects an int (e.g. write 0.4 not 4):\n{details}"
    )


def _load_settings_yaml() -> dict:
    """Parse config/settings.yaml. Returns ``{}`` when missing, empty, or unreadable
    (unreadable is warned via :func:`_warn_settings_unreadable`, never silent).
    An int written for a double-typed key raises (see
    :func:`_validate_settings_param_types`)."""
    path = _settings_yaml_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        _warn_settings_unreadable(path, error)
        return {}
    if not isinstance(data, dict):
        return {}
    _validate_settings_param_types(data)
    return data


def settings_params() -> list:
    """config/settings.yaml as a path list to splat into ``parameters=[pkg_yaml, *settings_params()]``
    (layers last, wins). Returns ``[]`` when the file is absent or empty."""
    if not _load_settings_yaml():
        return []
    return [str(_settings_yaml_path())]


def _settings_global_params() -> dict:
    """Flatten settings.yaml's ``/**`` ros__parameters block to dotted keys for the nav remap."""
    glob = _load_settings_yaml().get("/**", {})
    params = glob.get("ros__parameters", {}) if isinstance(glob, dict) else {}
    return _flatten_params(params, "") if isinstance(params, dict) else {}


def load_costmap_rewrites() -> dict:
    """Costmap inflation knobs from settings.yaml's ``/**`` inflation_layer block, for
    nav2 ``RewrittenYaml``. Returns ``{}`` when unset (costmap.yaml then used unchanged)."""
    g = _settings_global_params()
    out: dict = {}
    for key in ("inflation_radius", "cost_scaling_factor"):
        value = g.get(f"inflation_layer.{key}")
        if value is not None:
            out[key] = float(value)
    return out


# nav2 names velocity limits per plugin, so map the single ``nav.*`` override onto each schema.
# ``reverse_speed`` is the reverse-linear limit (negative); capped toward zero by _reverse_limit.
_NAV_MOTION_SCALAR_KEYS = {
    "mppi": {  # controller.yaml — plugin "InnateFollowPath"
        "max_speed": ("InnateFollowPath.vx_max",),
        "reverse_speed": ("InnateFollowPath.vx_min",),
        "max_angular_speed": ("InnateFollowPath.wz_max",),
    },
}


def _reverse_limit(default_reverse, lin) -> float:
    """Cap reverse-linear magnitude at the forward speed without enabling/increasing reverse.

    Reverse limits are <= 0, so ``max`` keeps the value closer to zero: ``nav.max_speed`` only
    ever reduces reverse speed, and a forward-only controller stays forward-only.
    """
    return max(float(default_reverse), -float(lin))


def _smoother_limit_overrides(lin, ang, defaults: dict) -> dict:
    """Build velocity_smoother [x, y, theta] limit lists from the nav override, keeping the
    lateral component from ``defaults``. Returns ``{}`` when those defaults are unavailable."""
    max_v = defaults.get("max_velocity")
    min_v = defaults.get("min_velocity")
    if not (isinstance(max_v, list) and isinstance(min_v, list) and len(max_v) >= 3 and len(min_v) >= 3):
        return {}
    max_v, min_v = list(max_v), list(min_v)
    if lin is not None:
        max_v[0] = float(lin)
        min_v[0] = _reverse_limit(min_v[0], lin)
    if ang is not None:
        max_v[2] = float(ang)
        min_v[2] = -float(ang)
    return {"max_velocity": max_v, "min_velocity": min_v}


def load_motion_limit_overrides(schema: str, defaults: dict | None = None) -> dict:
    """Remap settings.yaml's ``/** nav`` knob onto a nav2 node's velocity params. Returns
    ``{}`` when ``nav`` is unset (package YAML default then used unchanged).

    ``schema`` is ``"mppi"`` or ``"smoother"``. Pass ``defaults``
    (``load_yaml_param_defaults`` on the schema's package YAML) so the forward cap also caps
    reverse-linear toward zero; ``"smoother"`` also needs them to preserve the lateral component.
    """
    g = _settings_global_params()
    lin = g.get("nav.max_speed")
    ang = g.get("nav.max_angular_speed")
    if lin is None and ang is None:
        return {}

    if schema == "smoother":
        return _smoother_limit_overrides(lin, ang, defaults or {})

    mapping = _NAV_MOTION_SCALAR_KEYS.get(schema, {})
    defaults = defaults or {}
    overrides: dict = {}
    if lin is not None:
        for key in mapping.get("max_speed", ()):
            overrides[key] = float(lin)
        # Cap reverse-linear too (reduce-only, needs the package default).
        for key in mapping.get("reverse_speed", ()):
            default_reverse = defaults.get(key)
            if default_reverse is not None:
                overrides[key] = _reverse_limit(default_reverse, lin)
    if ang is not None:
        for key in mapping.get("max_angular_speed", ()):
            overrides[key] = float(ang)
    return overrides
