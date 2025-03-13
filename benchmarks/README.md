# Robot Directive Benchmarking System

This directory contains tools for benchmarking different directives sent to the robot simulation. The benchmarking system captures video frames, chat messages, and performance metrics during directive execution.

## Prerequisites

Before running any benchmarks, you must ensure that all components of the system are running in the correct order:

1. **Start the Robot**: First, start the robot component
2. **Start the Simulation**: Next, start the simulation environment
3. **Start the Brain**: Ensure the robot's brain is running properly
4. **Run Benchmarks**: Only after all the above components are running correctly

> **IMPORTANT**: The brain needs to run properly for the benchmarks to work correctly. If you encounter issues with the brain, you may need to reset it before running benchmarks.

## Overview

The benchmarking system consists of three main components:

1. **Benchmark Runner** (`benchmark_runner.py`): Runs a single benchmark test for a specific directive
2. **Multiple Benchmark Runner** (`run_benchmarks.py`): Runs multiple benchmarks in sequence
3. **Results Analyzer** (`analyze_results.py`): Analyzes benchmark results and generates reports

## Directory Structure

Each benchmark test creates a directory structure like this:

```
benchmarks/
  └── directive_name/
      ├── trial_1/
      │   ├── metadata.json       # Test parameters, timestamps, etc.
      │   ├── chat_log.json       # All chat messages during the test
      │   ├── metrics.json        # Performance metrics
      │   └── images/
      │       ├── first_person/   # First-person camera frames
      │       └── chase/          # Chase camera frames
      ├── trial_2/
      └── ...
  └── reports/                    # Generated reports and visualizations
```

## Usage

### Running a Single Benchmark

To run a single benchmark test:

```bash
./benchmarks/benchmark_runner.py "friendly_guide_directive" --duration 300 --trial 1
```

Parameters:
- First argument: The directive to send to the robot
- `--duration`: Test duration in seconds (default: 300)
- `--trial`: Trial number (default: 1)
- `--url`: Base URL for the API (default: http://localhost:8000)
- `--interval`: Frame capture interval in seconds (default: 1.0)

### Running Multiple Benchmarks

To run multiple benchmarks in sequence:

```bash
./benchmarks/run_benchmarks.py --directives "friendly_guide_directive" "security_patrol_directive" --trials 3
```

Parameters:
- `--directives`: List of directives to benchmark
- `--trials`: Number of trials for each directive (default: 1)
- `--duration`: Duration of each benchmark in seconds (default: 300)
- `--url`: Base URL for the API (default: http://localhost:8000)
- `--interval`: Frame capture interval in seconds (default: 1.0)

### Analyzing Results

To analyze benchmark results and generate reports:

```bash
./benchmarks/analyze_results.py --create_summary --create_charts --create_videos
```

Parameters:
- `--benchmark_dir`: Directory containing benchmark results (default: benchmarks)
- `--output_dir`: Directory to save reports (default: benchmarks/reports)
- `--create_summary`: Create summary report
- `--create_charts`: Create comparison charts
- `--create_videos`: Create timeline videos for all trials
- `--directive`: Specific directive to analyze (for videos)
- `--trial`: Specific trial to analyze (for videos)

## Available Directives

The system supports the following directives:

- `default_directive`
- `sassy_directive`
- `friendly_guide_directive`
- `security_patrol_directive`
- `elder_safety_directive`

Make sure to use these exact directive names when running benchmarks.

## Example Workflow

1. Start all required components in the correct order:
   - Start the robot
   - Start the simulation
   - Start the brain
   - Verify all components are running correctly

2. Run benchmarks for multiple directives:
   ```bash
   ./benchmarks/run_benchmarks.py --directives "friendly_guide_directive" "security_patrol_directive" "elder_safety_directive" --trials 3 --duration 180
   ```

3. Analyze the results:
   ```bash
   ./benchmarks/analyze_results.py --create_summary --create_charts --create_videos
   ```

4. View the generated reports in the `benchmarks/reports` directory

## Troubleshooting

- If the benchmark fails with "Simulation is not ready" error, make sure all components (robot, simulation, brain) are running.
- If the robot's brain reports failures, you may need to reset the brain before running benchmarks.
- If chat messages show "brain had a failure" repeatedly, restart the brain component.
- The chat log is saved in real-time, so you can monitor progress even if a benchmark is interrupted.

## Requirements

- Python 3.6+
- OpenCV (`pip install opencv-python`)
- Matplotlib (`pip install matplotlib`)
- Pillow (`pip install pillow`)
- NumPy (`pip install numpy`)
- Requests (`pip install requests`)
- WebSocket client (`pip install websocket-client`) 