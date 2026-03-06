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
