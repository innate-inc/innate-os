#!/usr/bin/env python3
"""
jetson_power.py – minimal INA3221 reader for any Jetson
======================================================
Works on:
  * JetPack‑4 Nano 4 GB (IIO driver, µV units)
  * JetPack‑5/6 Orin / Nano 4 GB (HWMON driver, mV units)

Public helpers
--------------
read_vin()   -> (V, A, W)     # autodetects module‑input rail
read_rail(label) -> (V, A, W) # explicit rail by inX_label
pretty_print(label=None)      # one‑liner / CLI (defaults to VIN)

Values returned: **floats in volts, amps, watts**.
No root or extra deps required.
"""
from __future__ import annotations

import pathlib
from typing import Dict, List, Tuple

__all__ = ["read_vin", "read_rail", "pretty_print"]

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------
_HW_ROOT = pathlib.Path("/sys/bus/i2c/drivers/ina3221")
_IIO_GLOB = "*-0040/iio:device*/in_voltage*_label"   # JP‑4 (Nano 4 GB)
_HWMON_GLOB = "*-0040/hwmon/hwmon*/in*_label"        # JP‑5/6 (Orin, Nano ≥JP‑5)

# Common aliases for the *input* rail across DTs / kernels
_VIN_CANDIDATES: List[str] = [
    "VIN_SYS_5V0",
    "VDD_IN",
    "POM_5V_IN",
    "VIN",
]

class SensorError(RuntimeError):
    """Raised when sensor files are missing or inconsistent."""


def _scan_labels() -> Dict[str, pathlib.Path]:
    """Return mapping {label: stem‑path‑without‑suffix}."""
    paths = list(_HW_ROOT.glob(_HWMON_GLOB)) or list(_HW_ROOT.glob(_IIO_GLOB))
    if not paths:
        raise SensorError("INA3221 sysfs nodes not found – is the driver loaded?")

    result: Dict[str, pathlib.Path] = {}
    for p in paths:
        label = p.read_text().strip()
        stem = p.with_name(p.name.rsplit("_label", 1)[0])  # drop the *_label suffix
        result[label] = stem
    return result


def _read_int(path: pathlib.Path) -> int:
    """Read an int from sysfs, raise SensorError if missing."""
    try:
        return int(path.read_text().strip())
    except FileNotFoundError as e:
        raise SensorError(f"Missing sysfs entry: {path}") from e


def _read_si_units(stem: pathlib.Path, legacy: bool) -> Tuple[float, float, float]:
    """Return (V, A, W) for the provided label‑stem."""
    if legacy:
        # IIO driver: µV / mA / mW – need scaling for volts
        mv = _read_int(stem.parent / (stem.name + "_input")) / 1000  # µV→mV
        ma = _read_int(stem.parent / (stem.name.replace("voltage", "current") + "_input"))
    else:
        mv = _read_int(stem.parent / (stem.name + "_input"))          # already mV
        # hwmon current file is currX_input (replace 'in'→'curr')
        ma = _read_int(stem.parent / (stem.name.replace("in", "curr") + "_input"))

    volts = mv / 1000.0
    amps  = ma / 1000.0

    # Power node may be absent on some kernels → compute fallback
    if legacy:
        mw_path = stem.parent / (stem.name.replace("voltage", "power") + "_input")
    else:
        mw_path = stem.parent / (stem.name.replace("in", "power") + "_input")

    if mw_path.exists():
        watts = _read_int(mw_path) / 1000.0  # mW→W (both drivers)
    else:
        watts = volts * amps  # fallback estimate

    return volts, amps, watts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_rail(label: str) -> Tuple[float, float, float]:
    """Return (V, A, W) for *label* (must match an inX_label string)."""
    labels = _scan_labels()
    stem = labels.get(label)
    if stem is None:
        raise SensorError(f"Rail '{label}' not found. Available: {sorted(labels)}")
    legacy = "iio:device" in stem.parent.as_posix()
    return _read_si_units(stem, legacy)


def read_vin() -> Tuple[float, float, float]:
    """Return the module input rail (best‑effort autodetect)."""
    labels = _scan_labels()
    for cand in _VIN_CANDIDATES:
        if cand in labels:
            legacy = "iio:device" in labels[cand].parent.as_posix()
            return _read_si_units(labels[cand], legacy)
    raise SensorError("Input rail not found – try read_rail(label) instead.")


def pretty_print(label: str | None = None) -> None:
    """Human‑friendly printout (also CLI entry)."""
    if label is None:
        v, a, w = read_vin()
        label = next(c for c in _VIN_CANDIDATES if c in _scan_labels())
    else:
        v, a, w = read_rail(label)
    print(f"{label}: {v:5.3f} V  {a*1e3:6.0f} mA  {w*1e3:7.0f} mW")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse, sys
    parser = argparse.ArgumentParser(description="Show Jetson INA3221 rail measurements")
    parser.add_argument("label", nargs="?", help="Rail label (omit for VIN)")
    args = parser.parse_args()
    try:
        pretty_print(args.label)
    except SensorError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
