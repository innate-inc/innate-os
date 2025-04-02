# Innate Simulator

This repository contains a simulation environment built with Genesis, featuring a web interface for visualization and interaction.

## Overview

This project integrates:

*   **Genesis:** For the core physics simulation and rendering.
*   **FastAPI:** As the backend web server.
*   **React:** For the frontend user interface.
*   **WebSockets:** For real-time communication between the frontend, backend, and agent.
*   **Auth0:** For secure user authentication.

## Installation

### Prerequisites

*   Node.js v18.x
*   Python 3.8+
*   Yarn package manager

### Backend Setup

1.  **Clone the repository:**
    ```bash
    git clone <repository-url> # Replace with your repo URL
    cd <repository-directory>
    ```
2.  **Create and activate a virtual environment:**
    ```bash
    python -m venv venv
    source venv/bin/activate # On Windows use `venv\Scripts\activate`
    ```
3.  **Install dependencies:**
    *   **macOS:** `pip install -r requirements.macos.txt`
    *   **Other OS:** (You might need to adjust dependencies) `pip install -r requirements.txt` *(Assuming a requirements.txt exists or needs creation)*

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

### 1. Start the Backend Server

From the project root directory:

```bash
python main_web.py [OPTIONS]
```

**Common Options:**

*   `-v` or `--vis`: Enable the Genesis simulation visualization window.
*   `--local`: Connect to a local agent server (e.g., ROS bridge running on `ws://localhost:9090`) instead of the cloud service.
*   `--need-oauth <true|false>`: Require OAuth authentication (default: `true`). Set to `false` for development **only** if Auth0 is not configured.
*   `--auth0-domain <your-domain>`: Your Auth0 domain (required if `--need-oauth true`).
*   `--auth0-audience <your-audience>`: Your Auth0 API identifier/audience (required if `--need-oauth true`).
*   `--log-everything`: Enable verbose logging for all agent model inputs/outputs.

### 2. Start the Frontend Development Server

In a *separate* terminal, from the `frontend` directory:

```bash
cd frontend
yarn dev
```

The frontend will typically be available at `http://localhost:5173`.

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
├── main_web.py            # Main FastAPI application entry point
├── README.md              # This file
├── requirements.macos.txt # Python dependencies for macOS
└── requirements.txt       # Python dependencies (if needed for other OS)
```

## Development Notes

*   **Backend Serves Frontend:** In the standard setup, the FastAPI backend serves the built React frontend from `frontend/dist/`.
*   **Frontend Dev Server:** For easier frontend development, run `yarn dev` in the `frontend` directory. This provides hot reloading but requires the backend to be running separately.
*   **Communication:** Components (simulation, agent bridge, web API) communicate via thread-safe queues defined in `src/shared_queues.py`.
*   **macOS Threading:** On macOS, the Genesis simulation runs in a separate thread managed by `gs.tools.run_in_another_thread` in `main_web.py`.

## Troubleshooting

*   **Genesis Viewer Issues:** If the visualization window doesn't appear or behaves strangely, try running `main_web.py` with the `-v` flag.
*   **WebSocket Connection:** If the frontend cannot connect to the agent: 
    *   Ensure the backend is running.
    *   If using a local agent bridge, ensure it's running and start the backend with `--local`.
    *   Check browser console logs for errors.
*   **Authentication Errors:** Verify Auth0 configuration in `.env` (frontend) and command-line arguments or environment variables (backend). Ensure the audience and domain match.

## VM Deployment

For specific instructions on deploying this application to the `sim.innate.bot` VM environment (including `nginx` configuration and process management with `tmux`), please refer to the dedicated guide:

[**docs/DEPLOYMENT.md**](docs/DEPLOYMENT.md) 