"""TypeSafe System One (Jev) over plain HTTP; the robot calls api.typesafe.ai directly, not via the proxy."""

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from innate_skills.demo_config import load_demo_config

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
CONFIG = Path(__file__).resolve().parents[1] / "config" / "typesafe.json"
OVERLOADED = (429, 529)
BACKOFF_S = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class Jev:
    api_key: str
    model: str


class JevBox(TypedDict):
    """A Box answer: edges as fractions of the image, measured from its top-left corner."""

    left: float
    top: float
    right: float
    bottom: float


def jev_from_config() -> Jev | None:
    """TYPESAFE_API_KEY wins over config/typesafe.local.json; None when neither holds a key."""
    config = load_demo_config(CONFIG)
    api_key = os.environ.get("TYPESAFE_API_KEY") or config.get("api_key", "")
    return Jev(api_key, config.get("model", "jev-preview")) if api_key else None


def ask_jev(jev: Jev, images: dict[str, str], questions: dict, sleep: Callable[[float], None], timeout: float) -> dict:
    """Answers keyed by question id; `images` maps state names to base64 JPEGs. Backs off only on 429/529."""
    refs = {name: str(uuid.uuid4()) for name in images}
    body = {
        "model": jev.model,
        "state": refs,
        "assets": {ref: {"type": "image", "format": "jpeg", "data_base64": images[name]} for name, ref in refs.items()},
        "questions": questions,
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {jev.api_key}",
            "Content-Type": "application/json",
            "TypeSafe-Danger-Zone-Images": "true",
        },
    )
    delays = iter(BACKOFF_S)
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)["answers"]
        except urllib.error.HTTPError as error:
            delay = next(delays, None)
            if error.code not in OVERLOADED or delay is None:
                raise
            sleep(delay)
