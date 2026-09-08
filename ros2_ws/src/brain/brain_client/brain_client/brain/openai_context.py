# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Experimental Responses adapter for the existing transactional brain context.

The loop still owns commits, cancellation, tools and image pruning. Native
Responses output (including encrypted reasoning and call IDs) travels alongside
the normalized decision, so it can be replayed unchanged on the next request.
No server-side conversation is created: an abandoned turn cannot advance it.
"""

from __future__ import annotations

import json
import uuid

from brain_client.brain.context import GeminiContext


def _schema(value):
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: item.lower() if key == "type" and isinstance(item, str) else _schema(item) for key, item in value.items()
    }


def _input(contents):
    items = []
    for content in contents:
        parts = content.get("parts", [])
        native = next((part["openaiOutput"] for part in parts if "openaiOutput" in part), None)
        if native is not None:
            items.extend(native)
            continue
        message = []
        assistant = content["role"] == "model"
        for part in parts:
            if "functionResponse" in part:
                result = part["functionResponse"]
                items.append(
                    {"type": "function_call_output", "call_id": result["id"], "output": json.dumps(result["response"])}
                )
            elif "inlineData" in part:
                data = part["inlineData"]
                message.append({"type": "input_image", "image_url": f"data:{data['mimeType']};base64,{data['data']}"})
            elif part.get("text"):
                message.append({"type": "output_text" if assistant else "input_text", "text": part["text"]})
        if message:
            items.append({"role": "assistant" if assistant else "user", "content": message})
    return items


class OpenAIContext(GeminiContext):
    def __init__(self, transport, *, service_tier="auto", **kwargs):
        super().__init__(transport, **kwargs)
        self._service_tier = service_tier
        # Group one conversation's requests without mixing independent robots.
        self._prompt_cache_key = "brain-" + uuid.uuid4().hex
        self.on_native_request = None

    def _prune(self):
        # Commit calls together with their results, even under tiny history caps.
        if self._history and any(
            item.get("type") == "function_call"
            for part in self._history[-1].get("parts", [])
            for item in part.get("openaiOutput", [])
        ):
            return
        super()._prune()

    def add_tool_outcomes(self, outcomes):
        super().add_tool_outcomes(outcomes)
        self._prune()

    def _stream_response(self, gemini_body, *, history_length):
        model = self._model
        history = _input(gemini_body["contents"][:history_length])
        body = {
            "model": model,
            "instructions": gemini_body["systemInstruction"]["parts"][0]["text"],
            "input": history + _input(gemini_body["contents"][history_length:]),
            "reasoning": {"effort": self._thinking_level},
            "service_tier": self._service_tier,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "parallel_tool_calls": False,
            "max_output_tokens": 4096,
        }
        if model == "gpt-6-astra" or model.startswith("gpt-6-astra-"):
            # The implicit end-of-input checkpoint includes the fresh wrist frame
            # and scratchpad, both replaced next turn. Cache the masked historical
            # prefix instead. Keep TWO boundaries so the previous write remains
            # explicitly eligible while the history grows. Native assistant and
            # tool items are replayed verbatim; only newly converted user blocks
            # receive markers. No cache state or history is mutated by generation.
            for item in [item for item in history if item.get("role") == "user"][-2:]:
                item["content"][-1]["prompt_cache_breakpoint"] = {"mode": "explicit"}
            instructions = body.pop("instructions")
            if instructions:
                body["input"].insert(
                    0,
                    {
                        "role": "developer",
                        "content": [
                            {
                                "type": "input_text",
                                "text": instructions,
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    },
                )
            body["prompt_cache_options"] = {"mode": "explicit"}
            body["prompt_cache_key"] = self._prompt_cache_key
        body["tools"] = [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": _schema(tool.get("parameters", {"type": "object", "properties": {}})),
                "strict": tool.get("strict", False),
            }
            for group in gemini_body.get("tools", [])
            for tool in group["functionDeclarations"]
        ]
        if self.on_native_request is not None:
            self.on_native_request(body)
        completed = False
        streamed_text = ""
        for event in self._transport(model, body):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                streamed_text += event["delta"]
                yield _chunk([{"text": event["delta"]}])
            elif kind in ("error", "response.failed", "response.incomplete"):
                raise RuntimeError(f"OpenAI Responses: {kind}")
            elif kind == "response.completed":
                response = event["response"]
                if response.get("status") != "completed":
                    raise RuntimeError("OpenAI Responses did not complete")
                output = response.get("output", [])
                parts = []
                for item in output:
                    if item["type"] == "function_call":
                        args = json.loads(item["arguments"])
                        if not isinstance(args, dict) or not item.get("call_id"):
                            raise RuntimeError("OpenAI Responses returned an invalid tool call")
                        parts.append({"functionCall": {"name": item["name"], "args": args, "id": item["call_id"]}})
                    elif item["type"] == "reasoning":
                        parts.extend(
                            {"text": summary["text"], "thought": True}
                            for summary in item.get("summary", [])
                            if summary.get("text")
                        )
                final_text = "".join(
                    c.get("text", "")
                    for item in output
                    if item["type"] == "message"
                    for c in item.get("content", [])
                    if c.get("type") == "output_text"
                )
                if not final_text.startswith(streamed_text):
                    raise RuntimeError("OpenAI Responses final text disagrees with streamed text")
                if remaining := final_text[len(streamed_text) :]:
                    yield _chunk([{"text": remaining}])
                has_text = bool(final_text)
                if not has_text and not any("functionCall" in part for part in parts):
                    raise RuntimeError("OpenAI Responses returned no actionable content")
                parts.append({"openaiOutput": output})
                usage = response.get("usage") or {}
                chunk = _chunk(parts)
                chunk["usageMetadata"] = {
                    "promptTokenCount": usage.get("input_tokens", 0),
                    "cachedContentTokenCount": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
                    "candidatesTokenCount": usage.get("output_tokens", 0),
                }
                details = usage.get("input_tokens_details") or {}
                if "cache_write_tokens" in details:
                    chunk["usageMetadata"]["cacheWriteTokenCount"] = details["cache_write_tokens"]
                yield chunk
                completed = True
        if not completed:
            raise RuntimeError("OpenAI Responses stream ended before completion")


def _chunk(parts):
    return {"candidates": [{"content": {"role": "model", "parts": parts}}]}
