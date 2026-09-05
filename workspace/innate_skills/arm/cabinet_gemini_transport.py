# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Adapt the shared cabinet policy to Gemini's chat API through Innate proxy."""

import copy
import json


class GeminiCabinetTransport:
    def __init__(self, client=None):
        if client is None:
            from innate_proxy import ProxyClient

            client = ProxyClient()
        if not client.is_available():
            raise ValueError("Set INNATE_SERVICE_KEY in the skills-server environment for Gemini")
        self.client = client

    def __call__(self, payload):
        messages = [{"role": "system", "content": payload["instructions"]}]
        for item in payload["input"]:
            if item.get("role") == "user":
                content = []
                for part in item["content"]:
                    if part["type"] == "input_image":
                        content.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
                    else:
                        content.append({"type": "text", "text": part["text"]})
                messages.append({"role": "user", "content": content})
            elif item.get("type") == "function_call":
                # Keep the native message intact, including Google's thought
                # signatures on tool calls, for multi-turn function calling.
                messages.append(copy.deepcopy(item["_gemini_message"]))
            elif item.get("type") == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        tool = payload["tools"][0]
        body = {
            "model": payload["model"],
            "messages": messages,
            "tools": [{"type": "function", "function": {k: tool[k] for k in ("name", "description", "parameters")}}],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            "parallel_tool_calls": False,
            "reasoning_effort": payload["reasoning"]["effort"],
            "max_tokens": payload["max_output_tokens"],
        }
        try:
            with self.client.request_stream(
                "gemini", "/v1/chat/completions", method="POST", json=body, timeout=45
            ) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"Gemini HTTP {response.status_code}; check proxy access, model and quota")
                data = json.loads(response.read())
        finally:
            self.client.close()
        choices = data.get("choices", [])
        if len(choices) != 1 or choices[0].get("finish_reason") not in ("stop", "tool_calls"):
            raise RuntimeError("Gemini response incomplete; no action executed")
        message = choices[0]["message"]
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("type") != "function":
            raise ValueError("Expected exactly one Gemini function call; no action executed")
        call = calls[0]
        return {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": call["function"]["name"],
                    "call_id": call["id"],
                    "arguments": call["function"]["arguments"],
                    "_gemini_message": message,
                }
            ],
        }
