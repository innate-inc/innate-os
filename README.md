<div align="center">

<!-- Add your banner image here -->
<!-- ![Innate Simulator Banner](./assets/banner.png) -->

# Innate Simulator

*A Genesis-powered simulation environment for robotics development and testing*

[![Discord](https://img.shields.io/badge/Discord-Join%20our%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/innate)
[![Documentation](https://img.shields.io/badge/Docs-Read%20the%20docs-blue?style=for-the-badge&logo=readthedocs&logoColor=white)](https://docs.innate.bot)
[![Website](https://img.shields.io/badge/Website-Visit%20us-orange?style=for-the-badge&logo=safari&logoColor=white)](https://innate.bot)
[![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)

</div>

---

> [!NOTE]
> **This simulator is in active development.** APIs and features may change. Join our Discord for updates and support.

---

## Overview

This repository contains two main components:

1. **Simulator** — A Genesis-based 3D simulation environment with a FastAPI backend and React frontend. The simulator communicates via WebSockets with the Innate operating system (running in a Docker container), allowing you to experiment with agents through a simple web interface.

2. **Benchmark Controller** — A test harness for running automated experiments and measuring agent performance under specific conditions.

Use this to develop and test agents for Innate robots — navigation, task execution, and embodied AI. The simulator currently focuses on mobility; manipulation capabilities are planned for future releases.

## Installation

### Prerequisites

*   Node.js v18.x
*   Python 3.11
*   Yarn package manager
*   [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Simulator Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url> # Replace with your repo URL
    cd <repository-directory>
    ```

2.  **Install dependencies with uv (recommended):**
    ```bash
    ./setup.sh
    source .venv/bin/activate
    ```
    This automatically detects your OS (macOS or Linux) and installs the appropriate dependencies.

3.  **Alternative: Manual setup with pip:**
    ```bash
    python3.11 -m venv .venv
    source .venv/bin/activate
    # macOS:
    pip install -r requirements.macos.txt
    # Linux/Ubuntu:
    pip install -r requirements.ubuntu.txt
    ```

4.  **Download scene data:**
    The simulator requires ReplicaCAD scene data. See [`data/README.md`](data/README.md) for detailed download instructions.

    Required datasets:
    - `data/ReplicaCAD_baked_lighting/` — Pre-baked lighting stages
    - `data/ReplicaCAD_dataset/` — Object meshes for collision

### Frontend Setup

1.  **Navigate to the frontend directory:**
    ```bash
    cd frontend
    ```
2.  **Install dependencies:**
    ```bash
    yarn install
    ```

## Running the Application

> [!IMPORTANT]
> The simulator connects to the **[Innate OS](https://github.com/innate-inc/innate-os)** running locally in Docker on `ws://localhost:9090`.
>
> **Before starting the simulator:**
> 1. Start the **[Innate OS](https://github.com/innate-inc/innate-os)** in Docker
> 2. (Optional) Start the **[Innate Cloud Agent](https://github.com/innate-inc/innate-cloud-agent)** locally — only needed if the Innate OS is configured to use a local agent instead of the cloud service
>
> See the linked repositories for setup instructions.

### 1. Start the Simulator

From the project root directory:

```bash
# Recommended for development
python main.py --vis --log-everything --need-oauth false
```

**Options:**

- `--vis` — Enable the Genesis simulation visualization window
- `--need-oauth false` — Disable authentication for local development
- `--log-everything` — Verbose logging for agent inputs/outputs
- `--no-web` — Run without the web server (headless mode)
- `--auth0-domain` / `--auth0-audience` — Auth0 config (required if OAuth enabled)

### 2. Start the Frontend Development Server

In a *separate* terminal, from the `frontend` directory:

```bash
cd frontend
yarn dev
```

The frontend will typically be available at `http://localhost:5173`.

#### Optional: Direct Robot WebSocket + WebRTC

The frontend can connect directly to a robot ROSBridge endpoint (instead of proxying chat/video through the simulator backend):

```bash
# frontend/.env
VITE_DIRECT_ROBOT_WS=true
VITE_DIRECT_WEBRTC=true
VITE_ROBOT_WS_URL=ws://<robot-ip>:9090
```

When disabled (default), the frontend keeps using `VITE_WS_BASE_URL` and `VITE_SIM_BASE_URL` as before.

## Benchmarks

The `benchmarks/` directory contains a framework for evaluating agent performance across various task categories (navigation, task completion, real-time interruption, etc.).

See **[benchmarks/README.md](benchmarks/README.md)** for detailed instructions on running and analyzing benchmarks.

**Quick start:**

```bash
# Run a single benchmark
python benchmarks/benchmark_runner.py --config benchmarks/configs/navigation_test.yaml --trial 1

# Run all benchmarks in a category
python benchmarks/run_benchmarks.py --category navigation --trials 10
```

## Configuration

### Environment Configuration Files

Environment configurations are stored as JSON files in the `data/environments/` directory. These files define the base scene and the dynamic entities present within it.

**Structure:**

```json
{
  "environment_name": "Baked_sc0_staging_00", // Base static scene name
  "entities": [
    {
      "name": "unique_entity_name", // e.g., "walker_1", "casualty_1"
      "asset_path": "path/to/model.obj", // Relative to project root
      "poses": [
        {
          "time": 0.0, // Simulation time for this keyframe
          "position": [x, y, z],
          "orientation": [w, x, y, z] // Quaternion
        },
        {
          "time": 10.0, // Simulation time for the next keyframe
          "position": [x2, y2, z2],
          "orientation": [w2, x2, y2, z2]
        }
        // Add more poses here...
      ],
      "loop": false // Optional, defaults to false. If true, trajectory restarts after the last pose.
    }
    // Add more entities...
  ]
}
```

*   **Fixed Entities:** An entity with only one pose in the `poses` list will be considered fixed at that position/orientation.
*   **Moving Entities:** Entities with multiple poses will linearly interpolate (LERP for position, SLERP for orientation) between consecutive poses based on the current simulation time. The `loop` parameter determines if the trajectory restarts from the beginning after reaching the last pose's time.

### Authentication (Auth0)

This application uses Auth0 for handling user logins and securing API endpoints.

*   **Setup:** See [AUTH0_SETUP.md](AUTH0_SETUP.md) for detailed instructions on configuring your Auth0 tenant, application, and API.
*   **Development:** For local development without requiring login, start the backend with the `--need-oauth false` flag.

## API Endpoints

The backend exposes several API endpoints for controlling the simulation and interacting with the agent. Most configuration endpoints require authentication (an Auth0 Bearer token).

**Base URL:** `http://localhost:8000` (unless configured differently)

### Configuration & Control (`/config_api`)

*   **`POST /set_environment`**
    *   Sets the active environment by positioning pre-loaded entities.
    *   **Requires Auth Token.**
    *   **Request Body:** JSON object containing *either*:
        *   `config_name`: (String) The name of a configuration file (without `.json`) in `data/environments/`.
        *   `config`: (Object) A full environment configuration dictionary (matching the structure described above).
    *   **Example (using `config_name`):**
        ```bash
        curl -X POST http://localhost:8000/set_environment \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer YOUR_AUTH0_TOKEN" \
        -d '{
          "config_name": "lying_man_corner"
        }'
        ```
    *   **Example (using `config` object):**
        ```bash
        curl -X POST http://localhost:8000/set_environment \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer YOUR_AUTH0_TOKEN" \
        -d '{
          "config": {
            "environment_name": "Baked_sc0_staging_00",
            "entities": [
              {
                "name": "walker_1",
                "asset_path": "data/assets/walking_man/man.obj",
                "poses": [
                  {"time": 0.0, "position": [0.0, 0.0, 0.10], "orientation": [0.0, 0.7071, 0.0, 0.7071]}
                ],
                "scale": [1.0, 1.0, 1.0]
              }
            ]
          }
        }'
        ```

*   **`POST /reset_robot`**
    *   Resets the robot's position and orientation.
    *   **Requires Auth Token.**
    *   **Request Body (Optional):** JSON object
        ```json
        {
          "memory_state": "optional_state_to_load",
          "position": [x, y, z],
          "orientation": [w, x, y, z]
        }
        ```
        *   If `position` and `orientation` are provided, the robot resets to that pose.
        *   Otherwise, it resets to the default initial pose.
        *   `memory_state` can be used to load a specific agent memory state (if implemented).

*   **`POST /shutdown`**
    *   Gracefully shuts down the simulation backend.
    *   **Requires Auth Token.**

### Video & State (`/video_api`)

*   **`GET /video_feed`**: MJPEG stream of the robot's first-person camera.
*   **`GET /video_feed_chase`**: MJPEG stream of the chase camera.
*   **`GET /video_feeds_ready`**: Checks if the simulation and video feeds are initialized. Returns `{"ready": true/false, "message": "..."}`.
*   **`GET /get_robot_position`**: Returns the robot's current position and timestamp. `{"position": [x,y,z], "timestamp": float}`.
*   **`POST /set_directive`**: Sends a natural language directive to the agent. Request body: `{"text": "Your directive here"}`.

### Chat (`/chat_api`)

*   **`GET /`**: Serves the main React frontend (`index.html`).
*   **`GET /auth/user-info`**: Gets authenticated user details (ID, email, authorization status). Requires Auth token.
*   **`GET /is-connected/{user_id}`**: Checks if a user is connected via WebSocket. Requires Auth token.
*   **`WS /ws/chat`**: WebSocket endpoint for real-time chat between frontend and agent. Handles its own authentication via query parameters during connection setup.

## Project Structure

```
├── data/
│   ├── environments/      # Environment config JSON files
│   └── assets/            # 3D models for dynamic entities
│   └── ...                # Other simulation data (URDF, scene files)
├── frontend/
│   ├── src/               # React frontend source code
│   └── dist/              # Built frontend (served by backend)
│   └── ...                # Config files (package.json, vite.config.js, .env)
├── src/
│   ├── agent/             # Agent communication types, WebSocket bridge
│   ├── middleware/        # Authentication middleware
│   ├── routes/            # FastAPI API route definitions (config, video, chat)
│   ├── simulation/        # SimulationNode, utilities
│   └── shared_queues.py   # Inter-process/thread communication queues
├── venv/                  # Virtual environment (ignored by git)
├── .gitignore
├── AUTH0_SETUP.md         # Auth0 setup guide
├── main.py            # Main FastAPI application entry point
├── README.md              # This file
├── requirements.macos.txt # Python dependencies for macOS
└── requirements.txt       # Python dependencies (if needed for other OS)
```

## Development Notes

*   **Backend Serves Frontend:** In the standard setup, the FastAPI backend serves the built React frontend from `frontend/dist/`.
*   **Frontend Dev Server:** For easier frontend development, run `yarn dev` in the `frontend` directory. This provides hot reloading but requires the backend to be running separately.
*   **Communication:** Components (simulation, agent bridge, web API) communicate via thread-safe queues defined in `src/shared_queues.py`.
*   **macOS Threading:** On macOS, the Genesis simulation runs in a separate thread managed by `gs.tools.run_in_another_thread` in `main.py`.

## Troubleshooting

*   **Genesis Viewer Issues:** If the visualization window doesn't appear or behaves strangely, try running `main.py` with the `-v` flag.
*   **WebSocket Connection:** If the frontend cannot connect to the agent: 
    *   Ensure the backend is running.
    *   Ensure the Innate OS is running in Docker.
    *   Check browser console logs for errors.
*   **Authentication Errors:** Verify Auth0 configuration in `.env` (frontend) and command-line arguments or environment variables (backend). Ensure the audience and domain match.

## VM Deployment

For specific instructions on deploying this application to the `sim.innate.bot` VM environment (including `nginx` configuration and process management with `tmux`), please refer to the dedicated guide:

[**docs/DEPLOYMENT.md**](docs/DEPLOYMENT.md)

---

<div align="center">

**Built with ❤️ by [Innate](https://innate.bot) in Palo Alto, California**

[Discord](https://discord.gg/innate) • [Documentation](https://docs.innate.bot) • [Website](https://innate.bot)

</div>
