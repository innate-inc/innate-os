# Skill Identity & Directory Design

# Written by Karmanyaah

- A skill directory contains metadata.json.


## Physical Skills
### Behavior Server

Plays back trained skills

- Receives skill_dir

READ {METADATA.json}->.execution

READ nom. {SKILL_DIR}/{RUN_NUM}/**/*.{pt,pth}
(files specified in .execution)


### Training Server

- Receives skill_dir

READ metadata.json
RW .training_skill_id
WRITE .execution

READ {SKILL_DIR}/data/
WRITE/CREATE {SKILL_DIR}/{RUN_NUM}/

### Recorder Node


Record and play back stuff

READ/WRITE/CREATE {SKILL_DIR}/data/{dataset_metadata.json,episode_{NUM}.h5}

## Both Physical and Code Skills

### Skills Action Server

Handles execution of both physical skills and code skills.



### Brain Client


# Agent Written stuff

> **Note:** The original design below proposed using skill IDs (`user/name`) everywhere.
> The actual implementation took a simpler approach: **absolute directory paths** are used
> on all ROS interfaces, and skill creation (including ID → path resolution) was moved to
> skills_action_server. See "Changes Implemented" at the bottom for what was actually built.

## Skill Types

There are two fundamentally different kinds of skills:

### Code Skills
A `.py` file in a skills directory. Contains a class inheriting from `Skill` with `name`, `execute()`, and `cancel()`. Runs in-process inside skills_action_server. Written by developers.

### Physical Skills
A subdirectory containing `metadata.json`. Involve the robot's arm/body. Have a lifecycle:

1. **Creation** (skills_action_server) — app calls `create_physical_skill` with a display name. Server creates the directory under `~/skills/` with a kebab-case name and writes `metadata.json`.
2. **Recording** (recorder_node) — human demonstrates by puppeteering the arm. Recorder is activated with an absolute directory path and creates episode H5 files + `dataset_metadata.json` under `data/`.
3. **Training** (training_node → cloud) — episode data uploaded, neural network trained, checkpoint downloaded back.
4. **Execution** (skills_action_server → behavior_server) — runs the trained policy or replays recorded motion.

Physical skill subtypes:
- **Learned** (`"type": "learned"`) — ACT neural network policy inference
- **Replay** (`"type": "replay"`) — plays back a recorded H5 file's actions

### Directory structure of a physical skill
```
pick-socks/
├── metadata.json                    ← skill definition (name, type, execution config) — created by skills_action_server
└── data/
    ├── dataset_metadata.json        ← recording metadata (episode count, timestamps) — created by recorder_node
    ├── episode_0.h5
    └── episode_1.h5
```

### Ownership boundaries
- **skills_action_server** owns `metadata.json` (creation, reading for skill discovery)
- **recorder_node** owns `data/` subdirectory (`dataset_metadata.json` + episode H5 files)
- **training_node** owns run output directories and `.execution`/`.training_skill_id` in metadata

### Execution call chain
```
brain_client (LLM) → /execute_skill → skills_action_server
                                           │
                                 ┌─────────┴─────────┐
                           Code skill?          Physical skill?
                           Run execute()        Forward to behavior_server
                           in-process           via /behavior/execute
                                                     │
                                           ┌─────────┴─────────┐
                                       Learned?            Replay?
                                       Run ACT policy      Play H5 actions
```

### Physical skill creation flow (app → robot)
```
App (CreateSkillScreen)
  │
  ├─ 1. callROSService(/brain/create_physical_skill, { name: "Pick Socks" })
  │     → skills_action_server creates ~/skills/pick-socks/metadata.json
  │     → returns { success, skill_directory: "/home/user/skills/pick-socks" }
  │
  └─ 2. callROSService(/brain/recorder/activate_physical_primitive, { task_directory: "/home/user/skills/pick-socks" })
        → recorder_node activates recording for that directory
```

---

## Skill ID Format

Skill IDs follow the pattern: `<user>/<skill_name>` matching `[a-z0-9_-]+/[a-z0-9_-]+`.

- **Code skill ID**: `<user>/<filename_without_py>` (e.g. `innate-os/navigate_to_position`)
- **Physical skill ID**: `<user>/<directory_name>` (e.g. `local/pick-socks`)

### Character rules
- Allowed characters in both `<user>` and `<skill_name>`: `[a-z0-9_-]`
- All inputs must be stripped of leading/trailing whitespace
- When creating a skill:
  - Either `name` (display name) or `id` must be provided
  - If `name` is not provided → `name = skill_name` portion of the ID
  - If `id` is not provided → `skill_name` derived from name: spaces → dashes, all other special chars removed, uppercase → lowercase
  - `id` must start with `innate-os/` or `local/`
- The `name` field in `metadata.json` is the human-readable display name (free-form text, not constrained)
- The directory name on disk **is** the `<skill_name>` portion of the ID

### Special users

| User | Disk location | Description |
|------|--------------|-------------|
| `local` | `~/skills/<skill_name>/` | User-created skills (recording, custom code) |
| `innate-os` | `$INNATE_OS_ROOT/skills/<skill_name>/` | Built-in skills shipped with innate-os |

No other users are valid yet. Future: cloud-synced skills from other users.

### ID ↔ Path resolution

```
innate-os/<skill_name>  →  $INNATE_OS_ROOT/skills/<skill_name>/
local/<skill_name>   →  ~/skills/<skill_name>/
```

For code skills, the "directory" is the parent directory containing the `.py` file:
```
innate-os/navigate_to_position  →  $INNATE_OS_ROOT/skills/navigate_to_position.py
local/my-custom-skill        →  ~/skills/my-custom-skill.py
```

### Examples

| ID | Display Name | Path |
|----|-------------|------|
| `local/pick-socks` | Pick Socks | `~/skills/pick-socks/` |
| `innate-os/wave` | wave | `$INNATE_OS_ROOT/skills/wave/` |
| `innate-os/navigate_to_position` | navigate_to_position | `$INNATE_OS_ROOT/skills/navigate_to_position.py` |
| `local/arm-circle` | Arm Circle | `~/skills/arm-circle/` |

---

## Interface Decisions

### Actions

| Interface | Type | Route | Param | Format | Notes |
|-----------|------|-------|-------|--------|-------|
| `/execute_skill` | `ExecuteSkill` | brain_client → skills_action_server | `skill_id` | **ID** (`user/name`) | Was `skill_type` (bare name). skills_action_server resolves to path internally |
| `/behavior/execute` | `ExecuteBehavior` | skills_action_server → behavior_server | `skill_dir` | **Absolute path** | Was `behavior_name`. Internal interface — skills_action_server already knows the path, just pass it through. Fixes the ~/skills vs ~/innate-os/skills bug |

### Services — Skill Creation & Discovery

| Interface | Type | Server | Param | Format | Notes |
|-----------|------|--------|-------|--------|-------|
| `/brain/create_physical_skill` | `CreatePhysicalSkill` | skills_action_server | `name` | Display name | **NEW.** Creates dir + `metadata.json` under `~/skills/`. Returns `success`, `message`, `skill_directory` (abs path). Converts display name to kebab-case for directory name. |
| `/brain/get_available_skills` | `GetAvailableSkills` | skills_action_server | *(returns list)* | Response includes **name**, **type**, **directory** (abs path), **episode_count** per skill | Physical skills include `directory` field; code skills do not |
| `/brain/reload_primitives` | `Trigger` | skills_action_server | *(none)* | No change | Reloads all skills from disk |
| `/brain/reload_skills` | `ReloadSkillsAgents` | skills_action_server | `skills[]` | **IDs** | Was bare names |
| `/brain/reload_skills_agents` | `ReloadSkillsAgents` | brain_client | `skills[]` | **IDs** | Was bare names. Forwards to `/brain/reload_skills` |
| `/brain/reload` | `Trigger` | brain_client | *(none)* | No change | Full reload of everything |

### Services — Recording (recorder_node)

| Interface | Type | Param | Format | Notes |
|-----------|------|-------|--------|-------|
| `brain/recorder/activate_physical_primitive` | `ActivateManipulationTask` | `task_directory` | **Absolute path** | Activates recording for an existing skill directory. Does NOT create the directory or `metadata.json` — that's done by `create_physical_skill` first. |
| `brain/recorder/get_task_metadata` | `GetTaskMetadata` | `task_directory` | **Absolute path** | Returns enriched dataset metadata (episodes, timesteps, etc.) from `dataset_metadata.json` |
| `brain/recorder/load_episode` | `LoadEpisode` | `task_directory`, `episode_id` | **Absolute path** + int | Loads an episode for replay |
| `brain/recorder/new_episode` | `Trigger` | *(none)* | No change | Uses active task |
| `brain/recorder/save_episode` | `Trigger` | *(none)* | No change | |
| `brain/recorder/cancel_episode` | `Trigger` | *(none)* | No change | |
| `brain/recorder/stop_episode` | `Trigger` | *(none)* | No change | |
| `brain/recorder/end_task` | `Trigger` | *(none)* | No change | |
| `brain/recorder/play_replay` | `Trigger` | *(none)* | No change | |
| `brain/recorder/pause_replay` | `Trigger` | *(none)* | No change | |
| `brain/recorder/stop_replay` | `Trigger` | *(none)* | No change | |

### Services — Cloud Training (innate_training node)

| Interface | Type | Param | Format | Notes |
|-----------|------|-------|--------|-------|
| `~/submit_skill` | `SubmitSkill` | `skill_dir` | **Absolute path** | `name` param removed — read display name from `metadata.json` instead |
| `~/create_run` | `CreateRun` | `skill_dir` | **Absolute path** | No change |
| `~/download_results` | `DownloadResults` | `skill_dir`, `run_id` | **Absolute path** + int | No change |

### Topics

| Topic | Type | Publisher | Fields | Notes |
|-------|------|----------|--------|-------|
| `/brain/recorder/status` | `RecorderStatus` | recorder_node | `task_directory`, `episode_number`, `status` | Uses absolute path, not skill ID |
| `/brain/recorder/replay_status` | `ReplayStatus` | recorder_node | `task_directory`, `episode_id`, etc. | Uses absolute path |
| `~/job_statuses` | `TrainingJobList` | training_node | `training_skill_id`, `skill_name`, `skill_dir` | `training_skill_id` is the cloud server's UUID, separate from the local skill ID. `skill_dir` is the abs path. No change needed |

### Internal interfaces (not on ROS)

| Interface | Where | Param | Notes |
|-----------|-------|-------|-------|
| Agent `get_skills()` | agent Python classes | Returns **IDs** | Was bare names. Agents declare which skills they can use by ID |
| `register_primitives_and_directive` | brain_client → websocket | Each primitive includes **id**, **name** | LLM sees both; uses ID in tool calls |
| Hot reload watcher | brain_client | Maps file changes to **IDs** | Derives `innate-os/<stem>` or `local/<stem>` based on which directory the file is in |

### Removed

| Interface | Status |
|-----------|--------|
| `GetAvailablePrimitives.srv` | Removed (only in old zenoh logs) |
| `GetAvailableBehaviors.srv` | Removed from CMakeLists (file never existed on disk) |
| `/policy/execute` action | Legacy hardcoded inference node — not launched in production |
| `get_task_metadata_list` service | **Removed from recorder_node.** App now uses `get_available_skills` (skills_action_server) for the skill list and `get_task_metadata` for per-skill episode data. |
| `update_task_metadata` service | **Removed from recorder_node.** metadata.json is owned by skills_action_server, not recorder. |
| `skill_utils.cpp` / `skill_utils.hpp` | **Deleted.** ID↔path resolution functions (`validate_skill_id`, `resolve_skill_directory`, `skill_id_for_directory`, `normalize_skill_name_from_display`) are no longer used. Name→kebab-case conversion now lives in skills_action_server (Python). |
| `ManipulationTask.srv` | **Renamed** to `ActivateManipulationTask.srv`. Request changed from `{skill_id, name, task_description, mobile_task}` to just `{task_directory}`. |

---

## Changes Implemented

Summary of the refactoring that was actually implemented (vs. the original design above):

### Key design decision: directories instead of IDs on ROS interfaces

The original design proposed using `skill_id` (e.g. `local/pick-socks`) on all recorder_node interfaces. The actual implementation uses **absolute directory paths** instead. Rationale:
- Recorder only needs the path — it never resolves IDs
- Skills_action_server already knows the path from `get_available_skills`
- Fewer moving parts; no ID↔path resolution needed in C++

### What changed

**Skill creation split into two steps:**
- `create_physical_skill` (skills_action_server, Python) — creates directory + `metadata.json`, returns abs path
- `activate_physical_primitive` (recorder_node, C++) — activates recording at a given directory path

**Recorder node simplified:**
- Only owns `data/` subdirectory: `dataset_metadata.json` + episode H5 files
- No longer creates `metadata.json`, no longer resolves skill IDs
- Removed services: `get_task_metadata_list`, `update_task_metadata`
- Removed: `skill_utils` dependency, `additional_skill_directories`, `reload_skills` client calls from `end_task`
- `RecorderStatus` topic publishes `task_directory` (abs path) instead of `skill_id`

**TaskManager simplified:**
- Removed: `start_new_task`, `resume_task`, `create_primitive_metadata`, `get_all_tasks_summary`, `update_task_metadata_by_directory`, `add_skill_directory`
- Kept: `start_new_task_at_directory`, `resume_task_at_directory`, `add_episode`, `end_task`, `get_task_metadata_by_directory`, `get_enriched_metadata_for_task`

**App (innate-controller-app) updated:**
- `CreateSkillScreen`: two-step flow — calls `create_physical_skill`, then `activate_physical_primitive` with returned directory
- `PhysicalSkillScreen`: receives `directory` from navigation params, calls `getTaskMetadata(directory)` directly instead of fetching all summaries
- `SkillsScreen`: passes `directory` from `get_available_skills` response when navigating to PhysicalSkill
- `RobotCoreContext`: removed `getAllTasksSummary`, `updateTaskMetadata`, `fetchTaskSummaries`, `taskSummaries`, `isLoadingTaskSummaries`
- `RecorderStatusContext`: uses `task_directory` instead of `skill_id`
- `types/ros.ts`: `RecorderStatus.skill_id` → `task_directory`, `Skill` gained optional `directory`, `TaskSummary` dropped `skill_id`/`task_description`/`mobile_task`

**Deleted files:**
- `skill_utils.cpp`, `skill_utils.hpp` — no longer compiled or referenced

## Bugs Fixed by This Redesign

1. **behavior_server only searched `$INNATE_OS_ROOT/skills/`** → now receives absolute path from skills_action_server, no path guessing
2. **recorder_node hardcoded `~/innate-os/skills`** → now receives abs path from caller, no path resolution needed
3. **metadata.json `name` ≠ directory name** → ID is always based on directory name; `name` is display-only, never used for path resolution
4. **recorder.yaml `data_directory: "~/skills"` vs behavior_server `~/innate-os/skills/`** → both resolve from ID; `local/` → `~/skills/`, `innate-os/` → `$INNATE_OS_ROOT/skills/`
