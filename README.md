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
```bash
git clone <repository-url>
cd genesis-sim-tests
```

2. Create and activate a virtual environment:
```bash
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.macos.txt
```

### Frontend Setup

1. Navigate to the frontend directory:
```bash
cd frontend
```

2. Install dependencies:
```bash
yarn install
```

## Running the Application

### Start the Backend

Run the main web application:
```bash
python main_web.py
```

Options:
- `-v` or `--vis`: Enable visualization
- `--local`: Connect to local agent server instead of cloud

### Start the Frontend Development Server

In a separate terminal:
```bash
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