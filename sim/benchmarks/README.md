# Agent Benchmarking Framework

A framework for evaluating agent performance across navigation, task completion, and other categories.

## Prerequisites

1. **Set up the environment** from the main directory. See [README.md](../README.md#installation)

2. **Start required components** in order:
   - **[Innate OS](../../innate-os)** — Start in Docker (`ws://localhost:9090`)
   - **(Optional) [Innate Cloud Agent](../../innate-cloud-agent)** — Only if using local agent
   - **Simulator** — `python main.py --vis ----disable-robot-collision`

3. **Set Google API key** for VLM-based evaluation:
   ```bash
   # Create benchmarks/.env with:
   GOOGLE_API_KEY=your_key_here
   ```

## Quick Start

```bash
# Run a single benchmark
python benchmarks/benchmark_runner.py --config benchmarks/configs/navigation_test.yaml --trial 1

# Run all benchmarks in a category
python benchmarks/run_benchmarks.py --category navigation --trials 5

# Run all benchmarks
python benchmarks/run_benchmarks.py --all --trials 10

# Analyze results
python benchmarks/analyze_results.py --create_summary --create_charts
```

## Usage

### Single Benchmark

```bash
python benchmarks/benchmark_runner.py --config <config.yaml> --trial <n>
```

| Option | Description | Default |
|--------|-------------|---------|
| `--config` | Path to YAML config file | Required |
| `--trial` | Trial number | 1 |
| `--url` | Simulator API URL | `http://localhost:8000` |

### Multiple Benchmarks

```bash
python benchmarks/run_benchmarks.py [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--configs file1.yaml file2.yaml` | Run specific config files |
| `--category <name>` | Run all configs in a category |
| `--all` | Run all available configs |
| `--trials <n>` | Number of trials per benchmark |
| `--send-email` | Send results via email |
| `--stop-simulator` | Stop simulator after completion |

### Analyze Results

```bash
python benchmarks/analyze_results.py [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--create_summary` | Generate summary report |
| `--create_charts` | Generate comparison charts |
| `--create_videos` | Generate timeline videos |
| `--directive <name>` | Analyze specific directive |
| `--trial <n>` | Analyze specific trial |

## Benchmark Categories

| Category | Description |
|----------|-------------|
| **Navigation** | Routing, obstacle avoidance, memory navigation |
| **Basic Task Completion** | Following instructions, environmental interaction |
| **Long-term Consistency** | Performance over extended periods |
| **Real-time Interruption** | Task switching, pause/resume |
| **Dynamism** | Adapting to environmental changes |
| **Discovery** | Exploring unknown environments |

## Results Structure

```
benchmarks/results/
└── <benchmark_name>/
    └── trial_<n>/
        ├── metadata.json      # Test parameters
        ├── chat_log.json      # Chat messages
        ├── metrics.json       # Performance metrics
        └── images/
            ├── first_person/  # FPV frames
            └── chase/         # Chase cam frames
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Simulation is not ready" | Ensure Innate OS and simulator are running |
| "Brain had a failure" | Restart the Innate OS container |
| Benchmark hangs | Check simulator logs, ensure `--no-web` is NOT set |

---

## Configuration Reference

Benchmark configs are YAML files in `benchmarks/configs/`. See existing configs for examples.

<details>
<summary>Full configuration schema</summary>

```yaml
name: "benchmark_name"
category: "category_name"
description: "What this benchmark tests"
goal: "Expected outcome"
directive: "directive_name"
duration: 600  # seconds

environment:
  name: "environment_name"
  initial_parameters:
    - robot_position: [x, y, z]  # Use z=0.05 for ground-safe spawn with Maurice
      robot_orientation: [qx, qy, qz, qw]

messages:
  - trigger_type: "time"
    time: 60
    text: "Message at 60 seconds"
  - trigger_type: "check"
    check_id: "some_check"
    text: "Message when check passes"

expectations:
  checks:
    - id: "location_check"
      type: "location"
      coordinates: [x1, y1, x2, y2]
    - id: "vlm_check"
      type: "vlm_verification"
      verification_prompt: "Did the robot do X?"
  success_criterion: "VLM prompt for success"
  stop_criterion: "VLM prompt for early stop"
```

</details>

<details>
<summary>Check types</summary>

| Type | Description |
|------|-------------|
| `location` | Verifies agent visited a bounding box area |
| `primitive` | Verifies a specific action was called |
| `compound` | Verifies action in specific location |
| `sequence` | Verifies ordered sequence of checks |
| `vlm_verification` | Uses Gemini to verify behavior from frames |

</details>

<details>
<summary>VLM Integration details</summary>

The benchmark runner uses Gemini to evaluate success/stop criteria by analyzing:
- Representative frames from both cameras
- Complete chat log with timestamps
- Check-specific context

Chat logs include `time_since_start` for timing analysis.

</details> 
