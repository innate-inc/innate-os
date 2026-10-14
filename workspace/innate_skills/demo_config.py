"""Deployment settings stay local; calibration and portable defaults stay tracked."""

import json
from pathlib import Path


def load_demo_config(path: Path) -> dict:
    """A local file replaces the whole default, including its endpoint credentials."""
    local = path.with_name(f"{path.stem}.local{path.suffix}")
    selected = local if local.exists() else path
    if not selected.exists():
        return {}
    config = json.loads(selected.read_text())
    if not isinstance(config, dict):
        raise ValueError(f"Expected a JSON object in {selected}")
    return config
