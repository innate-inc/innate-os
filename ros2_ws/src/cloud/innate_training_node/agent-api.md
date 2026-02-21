# innate_training_node — ROS 2 API

ROS 2 node (`innate_training`) for managing training skills, runs, uploads, and result downloads against the Innate training orchestrator. Lives on the robot.

## Node Name

`innate_training`

All topics and services are under this namespace (i.e. `~/…` expands to `/innate_training/…`).

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `server_url` | string | `$TRAINING_SERVER_URL` or built-in default | Training orchestrator URL |
| `service_key` | string | `$INNATE_SERVICE_KEY` | Robot service key for auth (required) |
| `auth_issuer_url` | string | `$INNATE_AUTH_URL` or built-in default | Auth issuer URL for token exchange |
| `poll_interval_sec` | float | `3.0` | Seconds between server polls for active runs |
| `status_publish_interval_sec` | float | `1.0` | Seconds between `~/job_statuses` publishes |

Both `server_url` and `service_key` are **required** — the node will abort if either is empty.

A `.env` file in the working directory is loaded automatically via `python-dotenv`.

---

## Published Topics

### `~/job_statuses` — `innate_cloud_msgs/TrainingJobList`

Periodic snapshot of every tracked skill and run. QoS: **transient-local**, reliable, depth 1.

```
builtin_interfaces/Time stamp
TrainingSkillStatus[]   skills
```

#### `TrainingSkillStatus`

```
string   skill_id
string   skill_name
string   skill_dir               # absolute path on robot (empty if unknown)

bool             has_active_transfer   # true while an upload is in progress
bool             transfer_done         # true once an upload completed (persistent)
TransferProgress active_transfer       # upload progress (valid when has_active_transfer)

TrainingRunStatus[] runs
```

#### `TrainingRunStatus`

```
# Status constants
uint8 STATUS_UNKNOWN=0
uint8 STATUS_WAITING_FOR_APPROVAL=1
uint8 STATUS_APPROVED=2
uint8 STATUS_REJECTED=3
uint8 STATUS_BOOTING=4
uint8 STATUS_RUNNING=5
uint8 STATUS_DONE=6
uint8 STATUS_DOWNLOADED=7

int32   run_id
uint8   status
string  daemon_state            # remote daemon sub-state
string  error_message

builtin_interfaces/Time started_at    # zero if unset
builtin_interfaces/Time finished_at   # zero if unset
string instance_type

bool             has_active_transfer   # true while a download is in progress
bool             transfer_done         # true once a download completed (persistent)
TransferProgress active_transfer       # download progress (valid when has_active_transfer)
```

#### `TransferProgress`

```
# Direction
uint8 UPLOAD=0
uint8 DOWNLOAD=1
uint8 direction

# Stage
uint8 STAGE_COMPRESSING=0
uint8 STAGE_UPLOADING=1
uint8 STAGE_DOWNLOADING=2
uint8 STAGE_VERIFYING=3
uint8 STAGE_DONE=4
uint8 STAGE_ERROR=5
uint8 stage

string filename
int32  file_index       # 1-based position in the batch
int32  file_total       # total files in the batch
int64  bytes_done
int64  bytes_total
bool   file_done        # this individual file is finished
bool   transfer_done    # entire transfer operation is finished
string error
string message          # human-readable summary
```

---

## Services

All service callbacks run on the ROS executor thread. Long operations (upload, download) are dispatched to background daemon threads; the service returns immediately with a success acknowledgement.

There are three services forming a linear workflow: **submit → create_run → download_results**.

### `~/submit_skill` — `innate_cloud_msgs/SubmitSkill`

Submit a skill **and** upload its data in one call. Idempotent: if the skill directory already has a `training_skill_id` in `metadata.json` (i.e. was previously submitted), the creation step is skipped and the node proceeds directly to uploading. This means callers never need to track whether a skill was already submitted.

Upload runs in a background thread; progress is reflected in `~/job_statuses` via `TransferProgress` (direction=UPLOAD).

**Request**

| Field | Type | Description |
|---|---|---|
| `skill_dir` | string | **Required.** Absolute path to the skill directory on the robot. |
| `name` | string | Optional human-readable skill name (defaults to directory name). Only used on first submit. |

**Response**

| Field | Type | Description |
|---|---|---|
| `success` | bool | Whether the operation succeeded (skill registered and upload started). |
| `skill_id` | string | UUID of the created (or existing) skill. |
| `message` | string | Human-readable status / error message. |

**Behaviour:**
1. If `<skill_dir>/metadata.json` already contains a `training_skill_id`, verify it against the server (fetch the skill). If the server confirms it exists and belongs to this user, reuse that ID (skip creation). If the server returns not-found or a permission error, log a warning and fall through to step 2 (the local ID is stale).
2. Otherwise, create the skill on the server and write the ID into metadata.
3. Start uploading data files in a background thread.

**Errors:** Fails if `skill_dir` is empty or relative, or if the server request fails.

---

### `~/create_run` — `innate_cloud_msgs/CreateRun`

Create a new training run for a previously submitted skill.

**Request**

| Field | Type | Description |
|---|---|---|
| `skill_dir` | string | **Required.** Absolute path to the skill directory. |
| `run_params` | `TrainingParams` | Training parameters (see below). |

**`TrainingParams`**

| Field | Type | Description |
|---|---|---|
| `preset` | string | **Required.** Preset name, e.g. `"act-default"`. |
| `env` | string[] | Optional environment overrides as `KEY=VALUE` strings. |
| `extra_json` | string | Optional JSON object for additional dynamic parameters. |

Currently known presets: `act-default`.

**Response**

| Field | Type | Description |
|---|---|---|
| `success` | bool | Whether the run was created. |
| `run_id` | int32 | Server-assigned run ID. |
| `message` | string | Human-readable status / error message. |

**Errors:** Fails if `skill_dir` is empty/relative, no `training_skill_id` in metadata, unknown preset, or malformed `extra_json`.

---

### `~/download_results` — `innate_cloud_msgs/DownloadResults`

Download results for a completed training run **and** activate the best checkpoint. Download runs in a background thread; progress appears in `~/job_statuses` via `TransferProgress` (direction=DOWNLOAD). Once the download finishes, the node automatically runs activation: it selects the best checkpoint (`*_step_*.pth`) and stats file (`*stats*.pt`) from `<skill_dir>/<run_id>/` and writes them into `metadata.json`'s `execution` block.

**Request**

| Field | Type | Description |
|---|---|---|
| `skill_dir` | string | **Required.** Absolute path to the skill directory (download destination). |
| `run_id` | int32 | **Required.** Non-negative run ID to download. |

**Response**

| Field | Type | Description |
|---|---|---|
| `success` | bool | `true` if the download was started. |
| `message` | string | Human-readable status / error message. |

**Post-download activation:** When the background download completes successfully, the node calls `activate_run(skill_dir, run_id)` which:
1. Finds the largest `*_step_*.pth` checkpoint in `<skill_dir>/<run_id>/`.
2. Finds a `*stats*.pt` file in the same directory.
3. Writes both paths (relative to `skill_dir`) into `metadata.json` under `execution.checkpoint` and `execution.stats_file`.

If activation fails (missing files, etc.), the download is still considered successful but a warning is logged.

**Errors:** Fails if `skill_dir` is empty/relative, `run_id` is negative, or no `training_skill_id` in metadata.

---

## Background Behaviour

- **Startup fetch:** On launch the node fetches all existing skills and runs from the server. Any runs already in `done` state are auto-downloaded (and activated) to their registered skill directory.
- **Polling:** A background thread polls active (non-terminal) runs every `poll_interval_sec` seconds. When a run transitions to `done`, an auto-download + activate is triggered.
- **Auto-download:** Each run is downloaded at most once per node lifetime (tracked by an internal set). Downloads go to the skill directory registered via `submit_skill` or `download_results`.
