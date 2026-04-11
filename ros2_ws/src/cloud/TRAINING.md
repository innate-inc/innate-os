# Training Guide

End-to-end reference for launching training runs from the robot. Covers the
full pipeline from skill submission through hyperparameter configuration to
result download.

## Overview

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

## Workflow

### 1. Submit a skill and upload data

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

### 2. Create a training run

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

### 3. Download results

Results are auto-downloaded when the training node detects a run has finished.
Manual download:

```bash
ros2 service call /innate_training/download_results innate_cloud_msgs/srv/DownloadResults \
  "{skill_dir: '/home/jetson1/skills/pick-cable', run_id: 1}"
```

## Presets

Presets are server-side templates that pre-fill training parameters. Any
parameter you pass explicitly overrides the preset value.

### `act-default`

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

## Training Hyperparameters

The `act-default` preset runs `cloud_run.sh`, which calls
`python3 -m act_test.train_dist` with parameters sourced from environment
variables. Pass these via the `env` field when creating a run.

### Configurable via environment variables

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

### Fixed architecture parameters (hardcoded in `train_dist.py`)

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

### Additional CLI flags (available via argparse, not exposed as env vars)

| Flag | Default | Description |
|---|---|---|
| `--shard_size` | 500 | Samples per WebDataset shard |
| `--warmup_steps` | 5% of max_steps | Override warmup step count |
| `--use-bf16` / `--no-bf16` | enabled | BF16 mixed precision training |
| `--use-compile` / `--no-compile` | enabled (but disabled by env) | torch.compile optimization |
| `--force-reconvert` | false | Force HDF5-to-WebDataset reconversion |

To pass these, override the `command` in `extra_json`.

## Examples

### Default run (no overrides)

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {preset: 'act-default', env: [], extra_json: ''}}"
```

Uses 4x H100, 120k steps, lr=5e-5, batch_size=96/gpu, chunk_size=30.

### Custom hyperparameters

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {
      preset: 'act-default',
      env: ['LEARNING_RATE=1e-4', 'BATCH_SIZE=64', 'MAX_STEPS=60000', 'CHUNK_SIZE=50'],
      extra_json: ''}}"
```

### Override infrastructure (fewer GPUs, more time)

```bash
ros2 service call /innate_training/create_run innate_cloud_msgs/srv/CreateRun \
  "{skill_dir: '/home/jetson1/skills/pick-cable',
    run_params: {
      preset: 'act-default',
      env: ['WORLD_SIZE=2'],
      extra_json: '{\"max_gpus\": 2, \"hours\": 10, \"budget\": 300}'}}"
```

### Full manual control (CLI only)

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

## Infrastructure Parameters

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

## Run Lifecycle

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

## Monitoring

The training node publishes status on `~/job_statuses` (transient-local,
default every 1 second). Each status message includes:

- Skill ID, name, and directory
- Upload/download progress (stage, file index, bytes transferred)
- Per-run status, daemon state, error messages, instance info

## Data Pipeline

Before upload, episode data goes through H.264 conversion:

1. Raw images in HDF5 (`/observations/images/*`) are encoded to H.264 MP4
2. Images are stripped from the HDF5 (keeping action, qpos, qvel data)
3. The smaller MP4 + stripped HDF5 files are uploaded

This requires a `dataset_metadata.json` in the skill's `data/` directory.
Without it, H.264 conversion is skipped and raw HDF5 files are uploaded
uncompressed.
