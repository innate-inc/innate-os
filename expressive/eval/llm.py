"""The eval's model calls: OpenAI Chat Completions (planner, grader, key-frame judge) and Gemini
``generateContent`` through the Innate proxy (video judge), both answering JSON against a schema.

Keys come from the environment; ``load_env`` fills in what is missing from the sim launcher's state file
(OPENAI_API_KEY, INNATE_SERVICE_KEY), never overriding what is already set.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auth_client import AuthProvider

ENV_FILE = Path(__file__).resolve().parents[2] / "sim" / "launcher" / ".state" / "innate-os.env"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
PROXY_URL = "https://proxy-v1.svc.innate.bot"
AUTH_URL = "https://auth-v1.svc.innate.bot"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
Json = dict[str, Any]
Chat = Callable[[list[dict[str, str]]], str]


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _post(url: str, body: Json, headers: dict[str, str], timeout_s: float, attempts: int = 4) -> Json:
    """POST JSON, retrying rate limits, server errors and dropped connections with backoff."""
    data = json.dumps(body).encode()
    for attempt in range(attempts):
        request = urllib.request.Request(url, data, {"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == attempts - 1:
                raise RuntimeError(f"HTTP {e.code} from {url}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        time.sleep(2.0 * 2**attempt)
    raise RuntimeError(f"no answer from {url}")


def _openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("needs OPENAI_API_KEY (environment or sim/launcher/.state/innate-os.env)")
    return key


def openai_chat(model: str, timeout_s: float = 120.0) -> Chat:
    """A planner chat function (messages -> reply text) on an OpenAI model."""
    key = _openai_key()

    def chat(messages: list[dict[str, str]]) -> str:
        reply = _post(OPENAI_URL, {"model": model, "messages": messages}, {"Authorization": f"Bearer {key}"}, timeout_s)
        return reply["choices"][0]["message"]["content"]

    return chat


def openai_json(model: str, content: list[Json], schema: Json, timeout_s: float = 180.0) -> Json:
    """One user turn (text and image parts) answered as JSON matching ``schema`` (strict structured output)."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "strict": True, "schema": schema}},
    }
    reply = _post(OPENAI_URL, body, {"Authorization": f"Bearer {_openai_key()}"}, timeout_s)
    return json.loads(reply["choices"][0]["message"]["content"])


@dataclass
class Proxy:
    """Gemini's native API behind the Innate proxy, authenticated with the robot's service key."""

    url: str = field(default_factory=lambda: os.environ.get("INNATE_PROXY_URL", PROXY_URL).rstrip("/"))
    _auth: AuthProvider | None = None

    def _token(self) -> str:
        auth = self._auth or self._login()
        until = auth.seconds_until_renewal()
        if until is not None and until <= 0:
            auth.token_needs_renewal = True  # the provider renews only when told to (or on a 401 via httpx)
        return auth.token

    def _login(self) -> AuthProvider:
        key = os.environ.get("INNATE_SERVICE_KEY")
        if not key:
            raise SystemExit("--judge gemini needs INNATE_SERVICE_KEY (environment or the sim's state file)")
        self._auth = AuthProvider(issuer_url=os.environ.get("INNATE_AUTH_URL", AUTH_URL), service_key=key)
        return self._auth

    def gemini_json(
        self, model: str, parts: list[Json], schema: Json, temperature: float, timeout_s: float = 180.0
    ) -> Json:
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        }
        url = f"{self.url}/v1/services/gemini/v1beta/models/{model}:generateContent"
        reply = _post(url, body, {"Authorization": f"Bearer {self._token()}", "User-Agent": "innate-robot"}, timeout_s)
        text = "".join(p.get("text", "") for p in reply["candidates"][0]["content"]["parts"] if not p.get("thought"))
        return json.loads(text)
