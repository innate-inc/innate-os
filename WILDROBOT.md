# WildRobot — Agent Generation Pipeline

This document explains how the end-to-end automatic agent generation pipeline works in `innate-os`.

---

## Overview

When a user sends a prompt to the robot that no existing agent can handle, the system automatically:

1. **Analyzes** the prompt against the known capability index
2. **Generates** Python code for the missing agent and its skills
3. **Hot-reloads** the new code into the running brain — no restart needed

```
User prompt
    │
    ▼
agent_orchestrator  ─── existing agent found? ──► route to specialist
    │
    │  (no match)
    ▼
semantic_skill_analyzer
    │  reads ~/.wildrobot/capabilities.json
    │  asks ollama/Gemini: "what's missing?"
    │  writes ~/.wildrobot/<uuid>-missing-skills.json
    ▼
agent_codegen
    │  reads missing-skills spec
    │  asks MiniMax API to write Python
    │  writes agents/<agent_id>.py + skills/<skill>.py
    ▼
HotReloadWatcher  (on_created event, ~1 s debounce)
    │
    ▼
New agent loaded ─► capabilities.json updated ─► user notified
```

---

## Components

### `semantic_skill_analyzer/`

Analyzes a natural-language prompt against the existing capability index.

**Inputs:**
- A user prompt (e.g. `"take a photo and email it"`)
- `~/.wildrobot/capabilities.json` — the current capability index

**Outputs:**
- `~/.wildrobot/<uuid>-missing-skills.json`

```json
{
  "existing_agents": ["demo_agent"],
  "missing_capabilities": {
    "photo_email_agent": {
      "prompt": "Captures a photo and emails it to the user.",
      "new_skills": [
        {"capture_photo": "Take a photo using the robot's camera"},
        {"send_email": "Send an email with an attachment"}
      ],
      "existing_skills": ["navigate_to_position"]
    }
  }
}
```

Returns empty `missing_capabilities` when existing agents already cover the request.

**Two backends (tried in order):**
1. **ollama** (`qwen3:1.7b`, local, no API key) — `analyze()`
2. **Google Gemini** (`gemini-3-flash-preview`, fallback) — `analyze_gemma()`

**Model requirements:** `qwen3:0.6b` is too small — it echoes the empty JSON template instead of reasoning about the prompt. **`qwen3:1.7b` minimum** is required for reliable gap detection. The default model is set via `MODEL` in `analyzer.py`.

**Ollama host:** Configured via the `OLLAMA_HOST` environment variable (defaults to `http://localhost:11434`). Set this to point at a remote GPU machine when needed.

---

### `agent_codegen/`

Generates Python Agent and Skill class files from a missing-capabilities spec.

**Inputs:**
- `missing_capabilities` dict (from analyzer output)
- MiniMax API key (`MINIMAX_API_KEY`)
- Reference examples: `agents/draw_triangle.py`, `agents/draw_circle_agent.py`
- Reference skill examples: `skills/draw_triangle.py`, `skills/draw_circle.py`
- Live ABC interfaces: `brain_client/agent_types.py`, `brain_client/skill_types.py`

**Outputs:**
- `agents/<agent_id>.py` — new Agent class
- `skills/<skill_name>.py` — one file per new skill

**Pipeline entry point:** `agent_codegen/pipeline.py`

```python
from agent_codegen.pipeline import run_pipeline, PipelineResult

result = run_pipeline("take a photo and email it")
# result.agent_id    → "photo_email_agent"
# result.agent_file  → "agents/photo_email_agent.py"
# result.skill_files → ["skills/capture_photo.py", "skills/send_email.py"]
```

**Debug output** is built in at `INFO` level. Use `-v` for full `DEBUG` output (raw model response, prompt preview, generated code preview).

---

### `~/.wildrobot/capabilities.json`

The capability index. Auto-written by `initialize_agents()` on every brain startup and after every hot reload.

Format:
```json
{
  "demo_agent": {
    "skills": ["innate-os/wave", "innate-os/navigate_to_position"]
  },
  "chess_agent": {
    "skills": ["innate-os/pick_up_piece_simple", "innate-os/head_emotion"]
  }
}
```

**Never edit manually** — it is regenerated automatically.

---

### `brain_client/agent_orchestrator.py`

Routes user prompts to the right specialist agent using keyword scoring.

- If a specialist is found → switch directive immediately
- If no specialist matches and the prompt is substantive (≥ 8 chars) → trigger codegen pipeline asynchronously

---

### Hot Reload (`brain_client/hot_reload_watcher.py`)

Watches `agents/` and `skills/` directories using `watchdog`. Fires on both file **modification** and **creation** events. After a 1-second debounce it reloads only the changed files — no full restart needed.

After a successful agent hot reload, `capabilities.json` is updated immediately so the next analyzer call sees the new agent.

---

## Determinism — When Does `in8 restart` Matter?

| Event | `capabilities.json` updated? | New agent available? |
|---|---|---|
| `innate start` / cold boot | Yes (via `initialize_agents`) | Yes |
| `innate restart` | Yes (via `initialize_agents`) | Yes |
| Codegen writes new `.py` | Yes (via hot reload callback) | Yes (~1 s) |
| `in8 wildrobot` CLI (brain not running) | No | On next boot |

**Hot reload is the primary path.** `in8 restart` is a guaranteed fallback if hot reload does not fire (e.g. `watchdog` not installed).

---

## CLI

### Generate a new agent from the command line

```bash
# Full generation (requires MINIMAX_API_KEY; uses ollama by default)
in8 wildrobot "make the robot patrol the room and alert on intruders"

# Analyze only — no files written
in8 wildrobot "make the robot take a selfie" --dry-run

# Force a specific ollama model
in8 wildrobot "make the robot take a selfie" --ollama-model qwen3:4b

# Force Gemini analyzer (requires GEMINI_API_KEY or GOOGLE_API_KEY)
in8 wildrobot "..." --use-gemma
```

### Run the pipeline module directly

```bash
# Standard run (ollama on localhost)
conda run -n local_llm python -m agent_codegen.pipeline \
    "pick up a tangerine" \
    --dry-run

# Explicit ollama host and model
conda run -n local_llm python -m agent_codegen.pipeline \
    "take a photo and email it" \
    --dry-run \
    --ollama-host http://localhost:11434 \
    --ollama-model qwen3:1.7b

# Via env vars (no flags needed)
OLLAMA_HOST=http://localhost:11434 OLLAMA_MODEL=qwen3:4b \
    conda run -n local_llm python -m agent_codegen.pipeline \
    "pick up a tangerine" --dry-run

# Verbose output — shows raw model response, prompt preview, code preview
conda run -n local_llm python -m agent_codegen.pipeline \
    "pick up a tangerine" \
    --dry-run -v

# Force Gemini instead of ollama
GEMINI_API_KEY=... conda run -n local_llm python -m agent_codegen.pipeline \
    "pick up a tangerine" \
    --dry-run --use-gemma
```

### Test gap analysis standalone

```bash
# Interactive — prompts for input if no argument given
conda run -n local_llm python3 scripts/test_skill_gap_analysis.py

# With explicit prompt
conda run -n local_llm python3 scripts/test_skill_gap_analysis.py "pick up a tangerine"

# With explicit prompt and model
conda run -n local_llm python3 scripts/test_skill_gap_analysis.py \
    "pick up a tangerine" --model qwen3:4b
```

---

## Required Environment Variables

| Variable | Required for | When |
|---|---|---|
| `MINIMAX_API_KEY` | Code generation | Always (for full generation) |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | Gemini analyzer | Fallback when ollama unavailable |
| `OLLAMA_HOST` | Ollama endpoint | Defaults to `http://localhost:11434` |
| `OLLAMA_MODEL` | Ollama model name | Defaults to `qwen3:1.7b` |
| `INNATE_OS_ROOT` | Path resolution | Defaults to `~/innate-os` |

For the systemd service, add to `/etc/systemd/system/ros-app.service`:
```ini
[Service]
Environment=MINIMAX_API_KEY=...
Environment=GEMINI_API_KEY=...
Environment=OLLAMA_HOST=http://172.17.30.138:11434
Environment=OLLAMA_MODEL=qwen3:1.7b
```

---

## Debugging

### Pipeline produces "task already covered" for everything

The most common cause is an **under-powered analyzer model**. `qwen3:0.6b` echoes the empty JSON template instead of reasoning. Symptoms:
- Raw response is exactly 69 chars: `` `{"existing_agents": [], "missing_capabilities": {}}` ``
- No `<think>` block in the output

**Fix:** Use `qwen3:1.7b` or larger. Check and update `MODEL` in `semantic_skill_analyzer/analyzer.py`.

### Pipeline stuck / ollama not responding

The `OLLAMA_HOST` default was previously hardcoded to a remote IP (`172.17.30.138`). It now reads from the `OLLAMA_HOST` env var and defaults to `localhost:11434`. If a run hangs, check that `OLLAMA_HOST` points to a reachable server.

### Pipeline produced empty result for a valid prompt

Run with `-v` to see the raw model response before JSON extraction:

```bash
conda run -n local_llm python -m agent_codegen.pipeline "your prompt" --dry-run -v
```

Look for:
- `raw model response (N chars) [thinking only — no JSON after </think>]` → model ran out of context reasoning and produced no output; try a larger model
- `filtered out (already in catalog): {'name'}` → postprocessing dropped a key that matched an existing skill name; this is usually correct but can be a false positive if the model used an existing skill name as the new agent name

---

## Adding a New Agent Manually

If you prefer to write code yourself, place your agent at `agents/<agent_id>.py`. The file watcher detects it within 1 second and loads it automatically. See `agents/draw_triangle.py` and `agents/draw_circle_agent.py` for reference examples.

Skills follow the same pattern — place them at `skills/<skill_name>.py`.
