# Training Manager

Browser-based UI for managing skills, datasets, and training runs on the
robot. Runs a local web server on the Jetson and provides a URL that can
be opened from any device on the same network.

## Features

- **Skills** — Browse and edit skill metadata (name, guidelines, execution config)
- **Datasets** — View episodes with video playback, submit/upload skills to the cloud,
  delete episodes (copy-with-exclusion), merge datasets from multiple skills
- **Training** — List runs with live status, view run details, create new runs with
  configurable hyperparameters and infrastructure params
- **Log terminal** — Collapsible panel streaming real-time backend logs

## Architecture

- **Backend**: FastAPI server (`training_manager/`) that delegates to the existing
  `training_client` `SkillManager` and `OrchestratorClient` for all cloud operations.
  Reads/writes skill directories on disk (`~/skills/`).
- **Frontend**: React + Vite + Tailwind SPA (`frontend/`), built into
  `training_manager/static/` and served by the FastAPI app.

## Quick Start (standalone)

### Prerequisites

- Python 3.10+
- Node.js 20+ (for building the frontend)
- The `training_client` package importable (either via colcon or `PYTHONPATH`)

### 1. Install Python dependencies

```bash
pip install fastapi uvicorn[standard]
```

### 2. Build the frontend

```bash
cd frontend
npm install
npm run build    # outputs to ../training_manager/static/
```

### 3. Set environment variables

```bash
export TRAINING_SERVER_URL="https://training-v1.innate.bot"
export INNATE_SERVICE_KEY="your-service-key"
export INNATE_AUTH_ISSUER_URL="https://auth-v1.innate.bot"  # optional
```

### 4. Run the server

```bash
# Via the training CLI (recommended)
python -m training_client.cli ui --skills-dir ~/skills --port 8080

# Or directly
python -m training_manager.server
```

Open the printed URL (e.g. `http://192.168.50.22:8080`) in your browser.

## ROS Workspace Install

When installed as part of the innate-os ROS workspace, everything is handled
automatically by `post_update.sh`:

1. `fastapi` and `uvicorn` are listed in `ros2_ws/pip-requirements.txt`
2. Node.js is installed via apt (NodeSource) and the frontend is built (step 6a)
3. `colcon build` packages `training_manager` as part of `cloud_clients`

After a successful build:

```bash
source ~/innate-os/ros2_ws/install/setup.bash
python -m training_client.cli ui --skills-dir ~/skills
```

## CLI Options

```
python -m training_client.cli ui [OPTIONS]

Options:
  --port INTEGER       HTTP port (default: 8080)
  --skills-dir PATH    Root skills directory (default: ~/skills)
  -s, --server TEXT     Orchestrator URL (or set TRAINING_SERVER_URL)
  -t, --token TEXT      Service key (or set INNATE_SERVICE_KEY)
  --issuer TEXT         Auth issuer URL (or set INNATE_AUTH_ISSUER_URL)
```

## Development

For frontend development with hot reload:

```bash
# Terminal 1: start the backend
python -m training_manager.server

# Terminal 2: start vite dev server (proxies /api to backend)
cd frontend
npm run dev
```

Vite dev server runs on port 5173 and proxies API calls to the backend on
port 8080.
