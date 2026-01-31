# Innate Simulator - Quick Start

## Prerequisites

- Python 3.11
- Node.js v18.x
- Yarn
- Git LFS (`brew install git-lfs && git lfs install`)

## Installation

### 1. Setup Python Environment

```bash
git clone <repository-url>
cd innate-sim
./setup.sh
source .venv/bin/activate
```

### 2. Download Scene Data

```bash
cd data
git clone https://huggingface.co/datasets/ai-habitat/ReplicaCAD_baked_lighting
git clone https://huggingface.co/datasets/ai-habitat/ReplicaCAD_dataset
cd ..
```

### 3. Build Frontend

```bash
cd frontend
yarn install
yarn build
cd ..
```

## Running the Simulation

### Terminal 1: Start the Simulator

```bash
source .venv/bin/activate
python main.py --vis --log-everything
```

### Terminal 2: Start the Frontend Dev Server

```bash
cd frontend
yarn dev
```

Open http://localhost:5173 in your browser.

## Command Line Options

| Option | Description |
|--------|-------------|
| `--vis` | Enable 3D visualization window |
| `--log-everything` | Verbose logging |
| `--no-web` | Headless mode (no web server) |
