# `brain_client` — package layout

The package is organised **by robot concept**, not by architecture layer. To find
code, start from what it *does*:

| Folder | What lives here |
|---|---|
| `nodes/` | The runnable ROS entry points (the only files with `main()`). Thin composition roots — they build collaborators, wire them, and spin. No behaviour. |
| `brain/` | The local agent loop: `agent` (look → think → act as one cancellable coroutine), `loop` (the dedicated-thread asyncio runtime it runs on), `context` (bounded Gemini conversation), `tools` + `transport` (declarations and the wire), pure `grounding` (pointed pixel → floor target), `memory_search` (recall over the spatial memory, context-cached) + `search_server` (the `/brain/search_memory` action skills call), and the system `prompt`. |
| `core/` | The activate/deactivate/reset state machine and directive switching (`lifecycle`), typed `config`, and the shared `state`. |
| `perception/` | Turning sensors into what the agent sees: `camera`, `map_image`/`map_capture`, `pose`/`pose_tracking`, `scan_health`, `gaze`. |
| `memory/` | Persistent per-map spatial memory: `store` (JSON index + JPEGs on disk), pure `selection` (which viewpoints earn a slot), `recorder` (the always-on ROS adapter that captures them). |
| `skills/` | The skill system: `registry`, `roster` (available + directive-active sets), `runner` (action lifecycle), `loader`, `hot_reload`, and the public `types` SDK base classes. |
| `agents/` | Directives/behaviours: `loader`, `initializer`, and the public `types` SDK base class. |
| `inputs/` | Input-device subsystem and its public `types` SDK base class. |
| `transport/` | Talking to the user: `chat` (history + chat-out + task status) and `tts`. |
| `robot/` | Low-level robot facades: `mobility`, `manipulation`, `head`. |
| `common/` | Cross-cutting leaf utilities: `logging`, `geometry`, `ros_services`, `script_paths`. |

## Two rules that keep this readable

**1. Dependency direction is one-way.**

```
nodes  ->  {brain, core}  ->  {perception, memory, skills, agents, inputs, transport, robot}  ->  common
```

A module never imports "upward" (e.g. `perception` never imports `core`).
`core/lifecycle` drives the agent loop without importing `brain/` — the node
injects it, so the two stay siblings rather than one depending on the other.

**2. Pure logic is separated from ROS glue by file name, not by folder.**

Within a concept folder, intention-revealing names tell you which is which:
`pose.py` is pure math; `pose_tracking.py` is the tf2/odom adapter. The *pure*
files — `perception/pose`, `brain/grounding`, `brain/prompt`, `brain/context`,
`skills/registry`, `core/config` — import **no `rclpy`** and are unit-tested
without a ROS runtime.

## Tests

Tests live in the package's top-level `test/` directory (wired into the ament
build). The pure ones — `test_local_brain.py`, `test_pose_math.py`,
`test_runner_deactivation.py` — need no ROS runtime beyond `brain_messages`, so
plain `pytest` runs them.
