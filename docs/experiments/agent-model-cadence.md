# Experimental core model and thinking cadence

This foundation is below the Household Orders solution PR. It reuses the generic
`TurnIntervals` API from PR #694's `3894f29fa`; it does not import Household skills,
route scoring or person identity. The original PR stays untouched.

## Selecting a model

In `config/settings.yaml`, merge these parameters into the existing node section,
then restart the brain node:

```yaml
brain_client_node:
  ros__parameters:
    brain_provider: openai
    openai_model: gpt-6-astra
    openai_reasoning_effort: low
    idle_turn_interval: 3.0
    supervision_turn_interval: 1.0
```

The default remains `brain_provider: gemini`, `gemini-3.6-flash`, `minimal`, and
3/5-second idle/supervision pauses. OpenAI is an explicit opt-in. Provider/model
settings are read at node startup; this experiment does not hot-swap a running
conversation. Auxiliary Gemini vision/memory skills retain their own model.

Official documentation checked September 5, 2026 names `gpt-6-astra`, with `low`
as its lowest reasoning effort. No separate “Astra light” model was found.
We interpret Axel's “GPT-6 Astra light” as **Astra with low reasoning**, without
substituting another model. It uses Responses, not Chat Completions tool calling.
See [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra) and
[model guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra).

The existing managed proxy is preferred (`openai`, `/v1/responses`). Without a
configured proxy, local development can use `OPENAI_API_KEY` in the launch
environment. Keys are never ROS parameters. Direct calls are disabled in public
demo mode; provider failures never silently switch accounts or models. Local key
setup is supplied by the separate provider-key PR.

**An OpenAI key alone does not enable every robot capability.** This switch only
changes the primary agent's thinking turns:

- `SearchMemory` still uses Gemini via `pick_rest` and `gemini_model`. With neither
  a configured Innate proxy nor `GEMINI_API_KEY`, the node does not start the
  `/brain/search_memory` action server. Memory recording remains available, but
  a directive offering `SearchMemory` cannot recall through that action.
- Microphone STT retains its separate configuration. ElevenLabs requires the
  Innate proxy; without it the microphone selects Gemini as its fallback. That
  fallback also needs `GEMINI_API_KEY`, so an OpenAI-only setup has no functioning
  microphone transcription through this path. This PR adds no OpenAI STT backend.
- Gemini-backed vision skills retain their existing credentials and models.

These dependencies were verified from
[`BrainClientNode._build_collaborators`](../../ros2_ws/src/brain/brain_client/brain_client/nodes/brain_client_node.py)
and [`MicroInput`](../../workspace/inputs/micro_input.py). The live comparison used
the managed proxy; it did **not** establish full standalone operation with only
an OpenAI key. Keep Gemini access when using those capabilities, or migrate them
in a separate change.

## Cadence and cancellation

A directive can override just the needed mode:

```python
from brain_client.agents.types import TurnIntervals


def get_turn_intervals(self):
    return TurnIntervals(supervision=1.0)
```

Values are finite positive seconds **after the previous turn completes**;
model latency is additional. `None` inherits the global setting. User/skill
and motion events preserve their existing early-wakeup behavior. The existing
one-second floor for feedback-driven turns and error backoff remain unchanged.
This is not a fixed Hz scheduler: slow inference never causes catch-up turns.

One coroutine owns committed history and dispatch. Stop now awaits its child
turn's cancellation cleanup before reporting completion, fixing a race exposed
by the new tests. As in the original worker-thread architecture, an abandoned
HTTP request can finish in the background (and incur provider cost); its muted
speech, tool calls, history and usage cannot commit. No server-side conversation
is advanced: OpenAI uses `store=false` and locally replays native output/call IDs
and encrypted reasoning. Normal cadence never overlaps inference requests;
user preemption can leave an orphaned request while a replacement starts.

Trace snapshots and completed turns include effective provider/model/effort;
completed turns include usage. OpenAI request traces contain the native body.
OpenAI output token counts include reasoning tokens. Full robot UI work is not
part of this experiment.

## Small live comparison

Twelve serial requests through the existing managed proxy, September 5, 2026:
two repeats each of idle, room description, and stopping an active navigation
skill; six requests per model. Both saw the same local 640x480 simulator living
room frame, core system prompt, self-portrait and state. Each probe began with
fresh local history. No skills were dispatched, no physical robot was used, and
no VM campaign was started. Responses used the default service tier.

| Configuration | Correct responses | Median completion | Mean completion | Estimated token cost, six calls |
| --- | ---: | ---: | ---: | ---: |
| Gemini 3.6 Flash / minimal | 6/6 | 1.072 s | 1.687 s | $0.01446 |
| GPT-6 Astra / low | 6/6 | 1.898 s | 1.779 s | $0.05096 |

Both waited silently when idle, named the living room, and called
`stop_current_skill` on request. Gemini also emitted a harmless `wait` alongside
its room-description speech. The initial speech-only heuristic marked that as
failure; the raw results preserve `strict_speech_only` separately and the outcome
assessment accepts that no-op. These tiny probes establish API usability and
basic discipline, **not Household Orders success rates** or statistically stable
latency. Gemini's first included call took 4.921 s; startup/tail latency matters.
Astra had cache hits on four calls and cache writes on two, so this is not a
controlled cold-cache or steady-state cost comparison.

Illustratively, median latency plus the 5-second default supervision pause is
6.07 s for Gemini and 6.90 s for Astra. Setting supervision to 1 second makes the
Astra cycle about 2.90 s in this sample, with correspondingly more requests. Those
are calculations, not measured closed-loop Household Orders timings.

Costs use returned usage with current standard list prices, not a billing
invoice. Gemini: $0.75/M input, $3.75/M output including thinking. Astra:
$10/M uncached input, $1/M cache reads, $12.50/M cache writes, $50/M output.
See [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing?hl=en) and the
[Astra model pricing](https://developers.openai.com/api/docs/models/gpt-6-astra).
The proxy's old Chat Completions analytics can record zero usage for Responses;
these estimates use the provider response directly.

Separate live three-turn Astra replay also passed: wait → tool outcome → room
answer → tool outcome → stop. The raw comparison and replay JSON files are next
to this report. Initial setup probes are excluded from the twelve-request table:
one local harness argument error, two successful Gemini probes, three Astra
parser failures, and three direct SSE diagnostics. The latter exposed the
managed proxy's `data: event: response.*` wrapping, now covered by a regression
test. Those setup requests may have billed; their complete usage was not captured.

Reproduce a bounded comparison (explicitly makes billed calls):

```bash
PYTHONPATH=ros2_ws/src/brain/brain_client:ros2_ws/src/cloud/clients/proxy-client:ros2_ws/src/cloud/clients/auth-client \
uv run --no-project --with httpx --with python-dotenv --with numpy \
python scripts/experiments/compare_brain_models.py --live \
  --env-file /path/to/owner/.env --image /path/to/living-room.jpg \
  --output /tmp/brain-comparison.json --repeats 2
```

## Verification and remaining limits

121 focused tests passed in the existing cached ROS Humble image, with external
networking disabled and pytest plugin autoload disabled (the image contains an
unrelated anyio/pytest version mismatch). Tests cover provider dispatch/defaults, invalid configuration, native
schema and image conversion, tool-call IDs and encrypted-reasoning replay,
image/history pruning, incomplete/failed streams, redacted transport failures,
proxy precedence/public-demo guards, late cancellation, and actual sequential
idle/supervision coroutine timing. Local HTTP-server tests exercise transport
routing. The managed provider comparison and replay exercise real API behavior.

The full ROS node has not been run with a live simulator UI in this foundation;
the Household task owns challenge integration above it. No production deployment
or robot motion was performed. Keep this PR DRAFT until Axel approves it.
