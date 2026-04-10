# Training Client

Python library for submitting training skills and runs to the Innate Training Orchestrator from a robot or user workstation. Designed as a core library with thin CLI and ROS 2 frontends.

## Quick Start

```bash
# Create a skill from your data directory (writes server-skill.json)
python -m training_client.cli submit ./skill

# Start a run with a preset (options pre-configured server-side)
python -m training_client.cli run ./skill --preset act-default

# Or specify everything manually
python -m training_client.cli run ./skill \
    --repo innate-inc/ACT-test \
    --ref lambda_refactor \
    -c "pip install -r requirements.txt; python3 -m act_test.train_dist --data-dir /data/dataset" \
    --hours 8 --gpu-type H100 --min-gpus 1 --max-gpus 2 \
    --budget 200
```

### Monitor and download results

```bash
# Poll until the run finishes
python -m training_client.cli watch ./skill <RUN_ID>

# Download results
python -m training_client.cli download ./skill <RUN_ID>
```

## Authentication

By default the client authenticates via **OIDC** (OpenID Connect).  The
`--token` / `INNATE_SERVICE_KEY` value is treated as a **service key** and
exchanged for a short-lived JWT through the issuer at
`https://auth-v1.innate.bot` (the default `--issuer`).

- **OIDC discovery** is lazy — the `/.well-known/openid-configuration`
  endpoint is only called on the first request, not at startup.
- **Automatic renewal** — if the orchestrator responds with
  `401 Unauthorized` and `WWW-Authenticate: Bearer error="invalid_token"`
  (per [RFC 6750](https://datatracker.ietf.org/doc/html/rfc6750)), the
  client transparently fetches a new JWT and retries the request.

### Development / plain bearer mode

To skip OIDC and pass `--token` directly as a bearer token, set the issuer
to an empty string:

```bash
# Via env var
export INNATE_AUTH_ISSUER_URL=""

# Or on the command line
python -m training_client.cli --issuer "" submit ./skill
```

In Python:

```python
from training_client.main import create_manager

manager = create_manager(
    server_url="http://localhost:8000",
    auth_token="dev_innate_service_key_abcde",
    auth_issuer_url="",  # plain bearer, no OIDC
)
```

## CLI Usage (`cli.py`)

```bash
# Submit a new skill (creates skill, compresses + uploads, creates run)
python -m training_client.cli submit /path/to/source/dir \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY" \
    --name "my-skill" \
    --repo myorg/train-script \
    --ref main \
    --command "python train.py"

# Upload data to an existing skill
python -m training_client.cli upload SKILL_ID /path/to/source/dir \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY"

# Check run status
python -m training_client.cli status SKILL_ID RUN_ID \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY"

# Poll until done (default 20s interval)
python -m training_client.cli watch SKILL_ID RUN_ID \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY" \
    --interval 20

# Download results
python -m training_client.cli download SKILL_ID RUN_ID \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY"

# List skills
python -m training_client.cli skills \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY"

# List runs for a skill
python -m training_client.cli runs SKILL_ID \
    --server https://orchestrator.example.com \
    --token "$INNATE_SERVICE_KEY"
```

## Architecture

```
training_client/
├── main.py               # Minimal — instantiates SkillManager, nothing else
├── cli.py                # Click CLI (innate-train)
├── training_node.py      # ROS 2 Humble lifecycle node (optional rclpy dep)
├── pyproject.toml
└── src/
    ├── __init__.py
    ├── types.py           # Dataclasses: ProgressUpdate, SkillInfo, RunInfo, ClientConfig
    ├── client.py          # HTTP wrapper for orchestrator REST API
    ├── compression.py     # Atomic zstd compression (tmp → rename)
    ├── uploader.py        # Per-file compress + upload with crash recovery
    ├── downloader.py      # Download results from run output dir
    └── skill_manager.py   # Core lifecycle: create skill → upload → create run → poll → download
```

All business logic lives in `src/`. The top-level `cli.py` and `training_node.py` are thin consumers that iterate the generators from `skill_manager.py` and present progress to the user.

## Core Concepts

### Skills & Runs

A **skill** is a training configuration — it holds a name, training parameters, and uploaded data files. A **run** is a single execution attempt of that skill on a GPU instance. Each skill can have multiple runs.

### Lifecycle (client-side)

```
create_skill ──► upload_files ──► create_run ──► poll_status ──► download_results
```

### Progress Updates (Generator Pattern)

`skill_manager.py` exposes generator functions that `yield` `ProgressUpdate` objects at each step. This lets both the CLI and ROS node consume the same core logic:

- **CLI** (`cli.py`): iterates the generator, prints updates to stdout.
- **ROS node** (`training_node.py`): iterates the generator, publishes updates on a topic.

### Atomic Zstd Compression

Since the process can die mid-compression, we use an atomic rename strategy:

```
source_file.bag  →  source_file.bag.zst.tmp   (compressing...)
                 →  source_file.bag.zst        (rename on success)
```

On recovery:
- `.zst.tmp` exists → delete it, re-compress from source.
- `.zst` exists → compression completed, skip straight to upload.
- Neither exists → compress from scratch.

Compressed files are placed alongside the source files in the source directory.

## API Endpoints Used

| Step | Method | Endpoint | Auth |
|------|--------|----------|------|
| Create skill | `POST` | `/skills` | User Bearer |
| List skills | `GET` | `/skills` | User Bearer |
| Get skill | `GET` | `/skills/{skill_id}` | User Bearer |
| Update skill | `PATCH` | `/skills/{skill_id}` | User Bearer |
| Request upload URLs | `POST` | `/skills/{skill_id}/files` | User Bearer |
| Create run | `POST` | `/skills/{skill_id}/runs` | User Bearer |
| List runs | `GET` | `/skills/{skill_id}/runs` | User Bearer |
| Get run | `GET` | `/skills/{skill_id}/runs/{run_id}` | User Bearer |
| Update run | `PUT` | `/skills/{skill_id}/runs/{run_id}` | User Bearer |
| List skill data files | `GET` | `/storage/list/{skill_id}` | User Bearer |
| List run output files | `GET` | `/storage/list/{skill_id}/{run_id}` | User Bearer |
| Upload file | `PUT` | `<signed_url>` | (presigned) |
| Download result | `GET` | `<signed_url>` | (presigned) |


## ROS 2 Node (`training_node.py`)

Lifecycle node for ROS 2 Humble. ROS is **not** required by the core library — only `training_node.py` imports `rclpy`.

### Interface

| Type | Name | Message Type | Description |
|------|------|-------------|-------------|
| Service | `~/submit_skill` | `std_srvs/srv/Trigger` | Create skill, upload data, create run |
| Service | `~/download_results` | `std_srvs/srv/Trigger` | Download results for active run |
| Topic | `~/progress` | `std_msgs/msg/String` | JSON progress updates (stage, skill_id, run_id) |
| Topic | `~/status` | `std_msgs/msg/String` | JSON run state + daemon_state on each poll |
| Parameter | `server_url` | `string` | Orchestrator URL |
| Parameter | `auth_token` | `string` | Service key (exchanged for JWT via OIDC) |
| Parameter | `auth_issuer_url` | `string` | OIDC issuer (default `https://auth-v1.innate.bot`, `""` = plain bearer) |
| Parameter | `poll_interval` | `double` | Status poll interval in seconds (default 20.0) |
| Parameter | `source_dir` | `string` | Default source directory for submissions |
| Parameter | `skill_name` | `string` | Optional skill name (default: directory name) |

### Lifecycle States

- **Configured**: Parameters set, client created.
- **Active**: Ready to accept skill requests.
- **Deactivated**: Paused.

## Training

End-to-end reference for launching training runs from the robot. Covers the
full pipeline from skill submission through hyperparameter configuration to
result download.

### Pipeline Overview

```
Robot (Jetson)                    Orchestrator Server                GPU Instance (RunPod)
──────────────                    ──────────────────                 ─────────────────────
submit_skill ──────────────────►  Create skill record
upload_files ──────────────────►  Store data in GCS
  (H.264 encode + upload)
create_run   ──────────────────►  Merge preset + params ──────────► Launch instance
                                  Store in DB                        git clone innate-inc/ACT
                                                                     Run cloud_run.sh
                                                                     Train with torch DDP
                                  Poll status ◄──────────────────── Heartbeat + logs
download     ◄─────────────────   Serve signed URLs    ◄──────────  Upload checkpoints
activate     (write to metadata)
```

### Workflow

#### 1. Submit a skill and upload data

**ROS service (from the robot):**

```bash
ros2 service call /innate_training/submit_skill innate_cloud_msgs/srv/SubmitSkill \
  "{skill_dir: '/home/jetson1/skills/pick-cable'}"
```

This creates the skill on the server (or reuses an existing one), converts
episode images to H.264, and uploads all data files in the background.

**CLI (standalone):**

```bash
python -m training_client.cli submit /home/jetson1/skills/pick-cable
```

#### 2. Create a training run

**ROS service:**

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {preset: 'act-default', env: [], extra_json: ''}}"
```

**CLI:**

```bash
python -m training_client.cli run /home/jetson1/skills/pick-cable --preset act-default
```

Runs start in `waiting_for_approval` status. An admin must approve them
before they launch on a GPU instance.

#### 3. Download results

Results are auto-downloaded when the training node detects a run has finished.
Manual download:

```bash
ros2 service call /innate_training/download_results innate_cloud_msgs/srv/DownloadResults \
  "{skill_dir: '/home/jetson1/skills/pick-cable', run_id: 1}"
```

### Presets

Presets are server-side templates that pre-fill training parameters. Any
parameter you pass explicitly overrides the preset value.

#### `act-default`

The only preset currently available.

| Parameter | Value |
|---|---|
| `repo` | `innate-inc/ACT` |
| `ref` | `main` |
| `command` | `["/training/project/cloud_run.sh"]` |
| `gpu_type` | `H100` |
| `min_gpus` | 4 |
| `max_gpus` | 4 |
| `hours` | 5 |
| `budget` | $200 |
| `checkpoint_patterns` | `/training/out/`, `/training/project/wandb/`, `/training/data/data/checkpoints/` |

### Training Hyperparameters

The `act-default` preset runs `cloud_run.sh`, which calls
`python3 -m act_test.train_dist` with parameters sourced from environment
variables. Pass these via the `env` field when creating a run.

#### Configurable via environment variables

| Env Variable | Default | Description |
|---|---|---|
| `LEARNING_RATE` | `5e-5` | Main optimizer learning rate |
| `LEARNING_RATE_BACKBONE` | `5e-5` | Vision backbone (ResNet18) learning rate |
| `BATCH_SIZE` | `96` | Batch size **per GPU** (effective = BATCH_SIZE x WORLD_SIZE) |
| `MAX_STEPS` | `120000` | Total training steps |
| `CHUNK_SIZE` | `30` | Action sequence length (prediction horizon) |
| `NUM_WORKERS` | `4` | DataLoader workers per GPU |
| `WORLD_SIZE` | `4` | Number of GPUs (auto-adjusts if fewer available) |
| `DATA_DIR` | `/training/data/data` | Path to training data on the instance |
| `OUTPUT_DIR` | `/training/out` | Output directory for checkpoints |

#### Fixed architecture parameters (hardcoded in `train_dist.py`)

These cannot be changed without modifying the training code.

| Parameter | Value | Description |
|---|---|---|
| `vision_backbone` | `resnet18` | Vision encoder architecture |
| `DIM_MODEL` | 512 | Transformer hidden dimension |
| `N_HEADS` | 8 | Number of attention heads |
| `N_ENCODER_LAYERS` | 4 | Transformer encoder depth |
| `N_DECODER_LAYERS` | 4 | Transformer decoder depth |
| `KL_WEIGHT` | 10.0 | VAE KL divergence loss weight |
| `USE_VAE` | True | Whether to use the variational autoencoder |
| `WEIGHT_DECAY` | 5e-4 | Optimizer weight decay |
| `WARMUP_STEPS` | 5% of MAX_STEPS | Linear LR warmup period |
| `MIN_LR_RATIO` | 0.1 | Cosine decay floor (LR decays to 1/10th) |
| `TRAIN_VAL_SPLIT` | 0.9 | 90% train / 10% validation |
| `CHECKPOINT_INTERVAL` | MAX_STEPS / 10 | Saves exactly 10 checkpoints |

#### Additional CLI flags (available via argparse, not exposed as env vars)

| Flag | Default | Description |
|---|---|---|
| `--shard_size` | 500 | Samples per WebDataset shard |
| `--warmup_steps` | 5% of max_steps | Override warmup step count |
| `--use-bf16` / `--no-bf16` | enabled | BF16 mixed precision training |
| `--use-compile` / `--no-compile` | enabled (but disabled by env) | torch.compile optimization |
| `--force-reconvert` | false | Force HDF5-to-WebDataset reconversion |

To pass these, override the `command` in `extra_json`.

### Infrastructure Parameters

These control the GPU instance, not the training script. Pass via `extra_json`
(ROS service) or CLI flags.

| Parameter | Default (preset) | Description |
|---|---|---|
| `repo` | `innate-inc/ACT` | GitHub repository to clone |
| `ref` | `main` | Git branch, tag, or commit SHA |
| `command` | `["/training/project/cloud_run.sh"]` | Entrypoint command |
| `gpu_type` | `H100` | GPU type |
| `min_gpus` | 4 | Minimum GPU count |
| `max_gpus` | 4 | Maximum GPU count |
| `hours` | 5 | Estimated duration (hours) |
| `budget` | 200 | Maximum cost in USD |
| `checkpoint_patterns` | (see preset) | Glob patterns for files to upload back |

### Run Lifecycle

```
waiting_for_approval ──► approved ──► booting ──► running ──► done ──► downloaded
                     └──► rejected
```

| Status | Meaning |
|---|---|
| `waiting_for_approval` | Run created, awaiting admin approval |
| `approved` | Admin approved, instance being provisioned |
| `booting` | GPU instance starting, installing dependencies |
| `running` | Training in progress |
| `done` | Training finished, checkpoints uploaded |
| `downloaded` | Results downloaded to robot |
| `rejected` | Admin rejected the run |

### Monitoring

The training node publishes status on `~/job_statuses` (transient-local,
default every 1 second). Each status message includes:

- Skill ID, name, and directory
- Upload/download progress (stage, file index, bytes transferred)
- Per-run status, daemon state, error messages, instance info

### Data Pipeline

Before upload, episode data goes through H.264 conversion:

1. Raw images in HDF5 (`/observations/images/*`) are encoded to H.264 MP4
2. Images are stripped from the HDF5 (keeping action, qpos, qvel data)
3. The smaller MP4 + stripped HDF5 files are uploaded

This requires a `dataset_metadata.json` in the skill's `data/` directory.
Without it, H.264 conversion is skipped and raw HDF5 files are uploaded
uncompressed.

### Training Examples

#### Default run (no overrides)

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {preset: 'act-default', env: [], extra_json: ''}}"
```

Uses 4x H100, 120k steps, lr=5e-5, batch_size=96/gpu, chunk_size=30.

#### Custom hyperparameters

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {
      preset: 'act-default',
      env: ['LEARNING_RATE=1e-4', 'BATCH_SIZE=64', 'MAX_STEPS=60000', 'CHUNK_SIZE=50'],
      extra_json: ''}}"
```

#### Override infrastructure (fewer GPUs, more time)

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {
      preset: 'act-default',
      env: ['WORLD_SIZE=2'],
      extra_json: '{\"max_gpus\": 2, \"hours\": 10, \"budget\": 300}'}}"
```

#### Full manual control (CLI only)

```bash
python -m training_client.cli run /home/jetson1/skills/pick-cable \
    --repo innate-inc/ACT \
    --ref main \
    -c "/training/project/cloud_run.sh" \
    --hours 8 --gpu-type H100 --min-gpus 4 --max-gpus 4 \
    --budget 200 \
    -e LEARNING_RATE=1e-4 \
    -e BATCH_SIZE=128 \
    -e MAX_STEPS=200000
```

## Dependencies

### Core (always required)
- `requests` — HTTP client
- `zstandard` — Python zstd bindings (fallback if system `zstd` binary unavailable)

### CLI
- `click` — CLI framework

### ROS 2 Node
- `rclpy` — ROS 2 Python client (system install, not pip)

## Configuration

The `ClientConfig` dataclass in `src/types.py` holds all configuration. At minimum you need:

```python
from training_client.src.types import ClientConfig
from training_client.src.skill_manager import SkillManager

config = ClientConfig(
    server_url="https://orchestrator.example.com",
    auth_token="your-innate-service-key",
)

manager = SkillManager(config)
```

All other parameters have sensible defaults (see `src/types.py`).
