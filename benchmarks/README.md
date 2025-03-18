# Agent Benchmarking Framework

This directory contains a framework for benchmarking agent performance across various categories of tasks. The benchmarks are designed to evaluate the agent's capabilities in six main categories, with three tasks per category.

## Benchmark Categories

1. **Long-term Consistency**
   - Evaluating the agent's ability to maintain performance over extended periods.
   - Includes tests for continuous operation, memory retention, and behavioral stability.

2. **Navigation**
   - Assessing the agent's ability to move efficiently and accurately in various environments.
   - Includes memory navigation, complex routing, and obstacle avoidance.

3. **Real-time Interruption**
   - Testing the agent's responsiveness to real-time changes and interruptions in task execution.
   - Includes task switching, pause/resume functionality, and handling contradictory instructions.

4. **Dynamism**
   - Evaluating the agent's ability to adapt to dynamic changes in the environment.
   - Includes adapting to moving obstacles, environmental changes, and modified goal parameters.

5. **Discovery**
   - Assessing the agent's capability to explore and map new environments or changes.
   - Includes mapping unknown environments, finding hidden objects, and detecting environment alterations.

6. **Basic Task Completion**
   - Ensuring the agent can complete fundamental tasks reliably.
   - Includes simple object manipulation, following multi-step instructions, and basic environmental interaction.

## Running Benchmarks

### Running a Single Benchmark

```bash
python benchmarks/benchmark_runner.py --config benchmarks/configs/path/to/config.yaml --trial 1
```

### Running Multiple Benchmarks

```bash
# Run specific config files
python benchmarks/run_benchmarks.py --configs configs/file1.yaml configs/file2.yaml --trials 10

# Run all configs in a category
python benchmarks/run_benchmarks.py --category 01_long_term_consistency --trials 10

# Run all available configs
python benchmarks/run_benchmarks.py --all --trials 10
```

## Configuration Format

Benchmark configurations use YAML files with the following structure:

```yaml
# Environment section - defines the simulation environment
environment:
  name: "environment_name"  # Which environment to use
  initial_parameters:
    - robot_position: [x, y, z]
      robot_orientation: [qx, qy, qz, qw]
      object_positions:
        object1: [x, y, z]
        object2: [x, y, z]

# Input section - what we provide to the agent
name: "benchmark_name"
category: "category_name"
description: "Description of what this benchmark tests"
goal: "Specific measurable outcome expected"
directive: "directive_name"  # Name of the predefined directive
duration: 600  # seconds

# Message scheduling
messages:
  # Time-based message
  - trigger_type: "time"
    time: 60  # seconds after start
    text: "Message text"
  
  # Check-based message
  - trigger_type: "check"
    check_id: "check_identifier"
    delay: 10  # seconds after check passes (optional)
    text: "Message text"

# Expectations section - how we verify success
expectations:
  checks:
    # Location check
    - id: "location_check_id"
      type: "location"
      coordinates: [x1, y1, z1, x2, y2, z2]  # Bounding box
    
    # Primitive check
    - id: "primitive_check_id"
      type: "primitive"
      primitive_name: "primitive_name"
      verification_prompt: "LLM prompt to verify arguments"
    
    # Compound check (primitive in location)
    - id: "compound_check_id"
      type: "compound"
      location: "location_check_id"
      action: "primitive_check_id"
      verification_prompt: "Verification prompt"
    
    # Sequence check
    - id: "sequence_check_id"
      type: "sequence"
      order: ["check_id1", "check_id2", "check_id3"]
      verification_prompt: "Verification prompt"
    
    # VLM verification check
    - id: "vlm_check_id"
      type: "vlm_verification"
      verification_prompt: "Did the robot perform a specific behavior correctly?"

  # Overall success criterion
  success_criterion: "VLM prompt describing what constitutes success"

  # Early stop criterion
  stop_criterion: "VLM prompt describing when the test can be stopped early"
```

### Key Components

1. **Environment**: Specifies the simulation environment and initial object/robot positions.

2. **Input**: Defines the task name, category, description, goal, directive, and duration.

3. **Messages**: Scheduled messages that can be triggered:
   - By specific times after the start of the benchmark
   - When specific checks pass (optionally with a delay)

4. **Expectations**: Defines success criteria through checks:
   - `location`: Verifies the agent visited a specific area
   - `primitive`: Verifies a specific primitive was called with correct arguments
   - `compound`: Verifies a primitive was called in a specific location
   - `sequence`: Verifies a sequence of checks occurred in the correct order
   - `vlm_verification`: Uses a VLM to verify a specific behavior

5. **Success/Stop Criteria**: VLM prompts that determine:
   - When the benchmark is considered successful
   - When the benchmark can be stopped early

## VLM Integration for Success and Stop Criteria

The benchmark runner now supports using Vision-Language Models (VLMs) such as GPT-4o to evaluate success and stop criteria. This evaluation is performed by:

1. **Frame Selection**: Selecting representative frames from first-person and chase cameras throughout the benchmark run.

2. **Structured Output**: Using GPT-4o with structured JSON output to evaluate if criteria are met, providing a boolean result and detailed explanation.

3. **Early Stopping**: Periodically checking if the stop criterion is met, allowing benchmarks to end early if completed or failed.

### Setting Up VLM Integration

Before using VLM integration, you need to:

1. Install required packages:
   ```bash
   pip install -r benchmarks/requirements.txt
   ```

2. Create a `.env` file in the `benchmarks` directory with your OpenAI API key:
   ```
   OPENAI_API_KEY=your_api_key_here
   ```
   
   A valid API key is already included in the `.env` file. If you need to use your own key, replace it in the file.

3. The VLM integration is now automatically enabled when running benchmarks. The system uses GPT-4o with structured JSON output to evaluate success and stop criteria.

### Using VLM Verification in Configurations

To use VLM verification in your benchmark configurations:

1. Add checks with type "vlm_verification" to verify specific behaviors:
   ```yaml
   expectations:
     checks:
       - id: "check_id"
         type: "vlm_verification"
         verification_prompt: "Did the robot perform X action correctly?"
   ```

2. Define success and stop criteria as natural language prompts:
   ```yaml
   expectations:
     success_criterion: "Did the robot complete all required tasks successfully?"
     stop_criterion: "Has the robot failed in a way that makes continuing pointless?"
   ```

3. The benchmark runner will use these prompts with captured frames to evaluate criteria.

### Time-Since-Start in Chat Logs

Chat logs now include a `time_since_start` field that shows seconds elapsed since the benchmark started, making it easier to analyze response times and message timing.

## Analyzing Results

Benchmark results are stored in the `benchmarks/results/` directory, organized by benchmark name and trial number.

To analyze results:

```bash
python benchmarks/analyze_results.py --benchmark benchmark_name
```

This will generate reports and visualizations based on the benchmark results.

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