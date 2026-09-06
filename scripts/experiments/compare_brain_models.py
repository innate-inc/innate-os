# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Small, explicitly paid managed-proxy replay. Never dispatches robot skills.

Run with brain_client, proxy-client and auth-client on PYTHONPATH and httpx,
python-dotenv and numpy installed. Uses existing managed credentials only;
no direct-key fallback, automatic retry or VM provisioning. See the report in
/docs/experiments/agent-model-cadence.md for the original evidence and limits.
"""

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from brain_client.brain.context import GeminiContext
from brain_client.brain.openai_context import OpenAIContext
from brain_client.brain.openai_transport import proxy_transport as openai_transport
from brain_client.brain.prompt import build_system_prompt, self_reference_turns
from brain_client.brain.tools import build_tools
from brain_client.brain.transport import proxy_transport
from brain_client.brain.utils import Event, EventKind, observation_text
from innate_proxy import ProxyClient

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--live", action="store_true", help="Permit 6 or 12 billed API requests")
parser.add_argument("--env-file", type=Path, required=True)
parser.add_argument("--image", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
args = parser.parse_args()
if not args.live:
    parser.error("This probe makes billed requests; pass --live explicitly")
cfg = dotenv_values(args.env_file)
proxy = ProxyClient(
    innate_service_key=cfg.get("INNATE_SERVICE_KEY"),
    proxy_url=cfg.get("INNATE_PROXY_URL"),
    auth_issuer_url=cfg.get("INNATE_AUTH_URL"),
)
assert proxy.is_available(), "No managed credentials configured"
image = args.image.read_bytes()
cases = [
    ("idle", None, []),
    ("describe", None, [Event('The user says: "What room are you in?"', kind=EventKind.USER)]),
    ("stop", "navigate_to_position", [Event('The user says: "Stop moving please."', kind=EventKind.USER)]),
]
rows = []
for repeat in range(args.repeats):
    for case, running, events in cases:
        for provider, model, effort, cls, transport in [
            ("gemini", "gemini-3.6-flash", "minimal", GeminiContext, proxy_transport(proxy)),
            ("openai", "gpt-6-astra", "low", OpenAIContext, openai_transport(proxy)),
        ]:
            capture = {}

            def traced(model, body, transport=transport, provider=provider, capture=capture):
                for event in transport(model, body):
                    if provider == "openai" and event.get("type") == "response.completed":
                        r = event["response"]
                        capture.update(
                            usage=r.get("usage"), returned_model=r.get("model"), service_tier=r.get("service_tier")
                        )
                    elif provider == "gemini":
                        if "usageMetadata" in event:
                            capture["usage"] = event["usageMetadata"]
                        if "modelVersion" in event:
                            capture["returned_model"] = event["modelVersion"]
                    yield event

            context = cls(
                traced,
                model=model,
                thinking_level=effort,
                max_history=60,
                max_image_turns=2,
                reference=self_reference_turns(),
            )
            message = context.user_message(
                observation_text(
                    now=datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc),
                    uptime_s=10,
                    pose=(0, 0, 0),
                    battery=None,
                    running_skill=running,
                    events=events,
                    has_wrist_frame=False,
                ),
                [image],
            )
            started = time.monotonic()
            first = []
            row = dict(repeat=repeat, case=case, provider=provider, model=model, effort=effort)
            try:
                response = context.generate(
                    message,
                    build_tools([], running, user_spoke=bool(events)),
                    build_system_prompt(None),
                    lambda text, first=first, started=started: (
                        first.append(time.monotonic() - started) if not first else None
                    ),
                )
                decision = context.absorb(message, response)
                calls = [c.name for c in decision.calls]
                passed = (
                    (calls == ["wait"] and not decision.speech)
                    if case == "idle"
                    else (bool(decision.speech) and all(c == "wait" for c in calls))
                    if case == "describe"
                    else ("stop_current_skill" in calls)
                )
                row.update(
                    status="ok",
                    pass_probe=passed,
                    speech=decision.speech,
                    calls=calls,
                    ttft=first[0] if first else None,
                    **capture,
                )
            except Exception as e:
                # Existing Gemini exception can echo an upstream body, so persist type only.
                row.update(status="error", error_type=type(e).__name__)
            row["seconds"] = round(time.monotonic() - started, 4)
            rows.append(row)
            print(json.dumps(row), flush=True)
            args.output.write_text(
                json.dumps({"image_sha256": hashlib.sha256(image).hexdigest(), "rows": rows}, indent=2)
            )
            if row["status"] == "error":
                raise SystemExit("Probe failed; stop campaign for investigation (no automatic fallback)")
