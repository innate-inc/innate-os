"""Demo: OpenAI chat completions via the service proxy.

Usage::

    PYTHONPATH=clients/proxy-client python -m demos.openai_chat "What is 2+2?"
    PYTHONPATH=clients/proxy-client python -m demos.openai_chat

Env vars (see .env.template):
    INNATE_PROXY_URL    — proxy service URL
    INNATE_AUTH_URL     — auth service URL (for OIDC JWT exchange)
    INNATE_SERVICE_KEY  — robot service key
    OPENAI_MODEL        — model override (default gpt-4o-mini)
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict

from proxy_client import ProxyClient

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")


async def chat_once(client: ProxyClient, message: str) -> None:
    """Send a single user message and print the reply."""
    resp = await client.request(
        service_name="openai",
        endpoint="/v1/chat/completions",
        method="POST",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are a helpful, concise assistant."},
                {"role": "user", "content": message},
            ],
            "max_tokens": 256,
        },
    )
    result: Dict[str, Any] = resp.json()

    content: str = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    print(f"🤖 {content}\n")
    print(
        f"  tokens: prompt={usage.get('prompt_tokens', '?')}  "
        f"completion={usage.get('completion_tokens', '?')}  "
        f"total={usage.get('total_tokens', '?')}\n"
    )


async def main() -> None:
    async with ProxyClient() as client:
        # One-shot mode
        if len(sys.argv) > 1:
            message = " ".join(sys.argv[1:])
            print(f"You: {message}")
            await chat_once(client, message)
            return

        # Interactive loop
        print(f"=== OpenAI Chat (model: {MODEL}) ===")
        print("Type a message and press Enter.  Ctrl-C to quit.\n")

        while True:
            try:
                line = input("You: ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if not line.strip():
                continue
            await chat_once(client, line)


if __name__ == "__main__":
    asyncio.run(main())
