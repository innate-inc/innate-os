# Genesis Simulation Tests

This repository contains a simulation environment built with Genesis, featuring a web interface for visualization and interaction.

## Overview

This project integrates:
- Genesis simulation environment
- FastAPI backend server
- React frontend
- WebSocket communication for real-time agent interaction

## Prerequisites

### macOS Requirements
- Node.js v18.x
- Python 3.8+
- Yarn package manager

## Installation

### Backend Setup (macOS)

1. Clone the repository:
```zsh
git clone <repository-url>
cd genesis-sim-tests
```

2. Create and activate a virtual environment:
```zsh
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```zsh
pip install -r requirements.macos.txt
```

### Frontend Setup

1. Navigate to the frontend directory:
```zsh
cd frontend
```

2. Install dependencies:
```zsh
yarn install
```

## Running the Application

### Start the Backend

Run the main web application:
```zsh
python main_web.py
```

Options:
- `-v` or `--vis`: Enable visualization
- `--local`: Connect to local agent server instead of cloud

### Start the Frontend Development Server

In a separate terminal:
```zsh
cd frontend
yarn dev
```

The frontend will be available at http://localhost:5173 by default.

## Project Structure

- `main_web.py`: Main entry point for the web application
- `src/`: Backend source code
  - `agent/`: Agent-related code and WebSocket bridge
  - `routes/`: API endpoints (video and chat)
  - `simulation/`: Simulation node and related components
  - `webrtc/`: WebRTC implementation for video streaming
  - `shared_queues.py`: Shared queue implementation for inter-process communication
- `frontend/`: React frontend application
  - `src/`: Frontend source code
  - `dist/`: Built frontend (served by the backend)
- `data/`: Data files for the simulation
- `requirements.macos.txt`: macOS-specific Python dependencies

## API Endpoints

The application exposes several API endpoints:
- Video streaming endpoints
- Chat API for agent interaction

## Development Notes

- The backend serves the frontend from the `frontend/dist` directory
- For local development, run the frontend dev server separately
- The application uses shared queues for communication between components
- On macOS, the simulation runs in a separate thread

## Troubleshooting

- If you encounter issues with the Genesis viewer, try running with the `-v` flag
- For WebSocket connection issues, use the `--local` flag to connect to a local agent server 

## VM Deployment

### Prerequisites
- Virtual Machine with access to sim.innate.bot domain
- Docker installed
- tmux for managing multiple sessions
- nginx for web server configuration

### Deployment Process

1. **Import Genesis Simulation and Maurice Production Version**

   Clone both repositories to the VM:
   ```zsh
   git clone <genesis-sim-repo-url>
   git clone <maurice-prod-repo-url>
   ```

2. **Start Maurice Prod**

   In the first ssh connection:
   ```zsh
   # Navigate to Maurice production directory
   cd <maurice-prod-directory>
   
   # Start the Docker container with brain components
   docker-compose -f docker-compose.prod.yml up -d
   docker compose -f docker-compose.dev.yml exec maurice zsh -l

   # Then follow the process in the repo to start the bridge and brain
   ```

3. **Setup Genesis Simulation Environment**

   In another ssh connection:
   ```zsh
   # Create a new tmux window
   tmux new
   
   # Navigate to Genesis simulation directory
   cd <genesis-sim-directory>
   
   # Start the simulation with local agent server and no visualization
   python main_web.py --local
   ```

4. **Nginx Configuration**

   Configure nginx to serve the application at sim.innate.bot:
   ```zsh
   # Edit nginx configuration
   sudo nano /etc/nginx/sites-available/default

   # Add the config from nginx/default.conf

   # Enable the site and restart nginx
   sudo ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/
   sudo nginx -t  # Test the configuration
   sudo systemctl restart nginx
   ```

5. **Access the Simulation**

   The simulation should now be accessible at http://sim.innate.bot

### Managing the Deployment

- To detach from a tmux session: Press `Ctrl+B` then `D`
- To reattach to a tmux session: `tmux attach -t session-name`
- To list all tmux sessions: `tmux ls`
- To stop the services, reattach to the respective tmux sessions and press `Ctrl+C` 