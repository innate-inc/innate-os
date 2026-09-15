"""Load bounded local calibration coefficients, never recordings or code."""

import json
import math
from pathlib import Path
from typing import Any


def load_profile(path: Path | str) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    profile = json.loads(path.read_text())

    def vector(value: object, length: int, bound: float) -> None:
        if (
            not isinstance(value, list)
            or len(value) != length
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > bound for v in value)
        ):
            raise ValueError("Invalid personal calibration coefficients")

    def matrix(value: object, rows: int, columns: int, bound: float) -> None:
        if not isinstance(value, list) or len(value) != rows:
            raise ValueError("Invalid personal calibration matrix")
        for row in value:
            vector(row, columns, bound)

    try:
        if profile["version"] != 1:
            raise ValueError("Unsupported personal calibration version")
        vector(profile["reference"], 9, 1.001)
        matrix(profile["rotation"]["matrix"], 2, 3, 4)
        responses = profile["rotation"]["response"]
        if len(responses) != 2:
            raise ValueError("Invalid rotation calibration")
        for response in responses:
            vector([response["deadzone"], response["negative"], response["positive"]], 3, 3)
            if not 0 <= response["deadzone"] <= 0.1 or min(response["negative"], response["positive"]) <= 0:
                raise ValueError("Invalid rotation gain")
        matrix(profile["position"]["matrix"], 3, 3, 2)
        matrix(profile["position"]["compensation"], 3, 5, 2)
        vector(profile["grip"]["knots"], 3, 5)
        if "rotationCompensation" in profile["grip"]:
            vector(profile["grip"]["rotationCompensation"], 4, 2)
        closed, neutral, opened = profile["grip"]["knots"]
        if not 0 <= closed < neutral < opened or not 0 < profile["grip"]["neutral"] < 1:
            raise ValueError("Invalid finger-gap calibration")
        workspace = profile["workspace"]
        vector(workspace["center"], 3, 0.5)
        vector(workspace["span"], 3, 0.15)
        vector(workspace["angles"], 5, math.pi)
        if min(workspace["span"]) <= 0 or not 0 <= workspace["grip"] <= 1:
            raise ValueError("Invalid personal workspace")
        if not 0.24 <= workspace["center"][0] <= 0.38 or not 0.08 <= workspace["center"][2] <= 0.20:
            raise ValueError("Personal workspace is outside this simulator")
        if "floor" in profile:
            floor = profile["floor"]
            if floor["version"] != 1 or not 0.15 <= floor["width"] <= 2:
                raise ValueError("Invalid floor calibration version or width")
            vector(floor["scales"], 8, 3)
            if min(floor["scales"]) <= 0:
                raise ValueError("Invalid floor feature scales")
            count = len(floor["centers"])
            if not 1 <= count <= 64:
                raise ValueError("Invalid floor calibration size")
            matrix(floor["centers"], count, 8, 6)
            matrix(floor["coefficients"], count, 5, 4)
            grip = floor["grip"]
            vector(grip["pitch"], 3, math.pi / 2)
            if not 0 == grip["pitch"][0] < grip["pitch"][1] < grip["pitch"][2]:
                raise ValueError("Invalid floor pitch knots")
            for key in ("closed", "neutral", "open"):
                vector(grip[key], 3, 6)
            for i in range(3):
                if not 0 <= grip["closed"][i] < grip["neutral"][i] < grip["open"][i]:
                    raise ValueError("Invalid floor finger-gap knots")
        if "yaw" in profile:
            yaw = profile["yaw"]
            if yaw["version"] != 1 or not 0.1 <= yaw["width"] <= 2:
                raise ValueError("Invalid yaw calibration version or width")
            count = len(yaw["centers"])
            if not 1 <= count <= 64:
                raise ValueError("Invalid yaw calibration size")
            matrix(yaw["centers"], count, 5, 6)
            vector(yaw["coefficients"], count, 4)
        return profile
    except (KeyError, TypeError) as error:
        raise ValueError("Incomplete personal calibration") from error
