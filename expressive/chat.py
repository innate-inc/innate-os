"""Chat functions for the planner on the host: OpenAI-compatible HTTPS endpoints, keys from the env.

gemini   GEMINI_API_KEY   Google's OpenAI-compatible endpoint
openai   OPENAI_API_KEY
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable

Chat = Callable[[list[dict[str, str]]], str]
ENDPOINTS = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "GEMINI_API_KEY",
        "gemini-3.6-flash",
    ),
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "gpt-5.4-mini"),
}


def endpoint_chat(provider: str, model: str | None = None, timeout_s: float = 60.0) -> Chat:
    url, key_env, default_model = ENDPOINTS[provider]
    key = os.environ.get(key_env)
    if not key:
        raise SystemExit(f"--chat {provider} needs {key_env} in the environment")

    def chat(messages: list[dict[str, str]]) -> str:
        body = json.dumps({"model": model or default_model, "messages": messages}).encode()
        request = urllib.request.Request(
            url, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read())["choices"][0]["message"]["content"]

    return chat
