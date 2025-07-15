# Benchmark Configuration System

This directory contains YAML configuration files for the benchmark runner. Each file defines a specific benchmark scenario with directives, messages, and timing.

## Configuration File Format

```yaml
name: "benchmark_name"
description: "Description of what this benchmark tests"
goal: "What you expect to happen or measure"
directive: "The directive to send to the robot"
duration: 120  # seconds
messages:
  - time: 10  # seconds after start
    text: "Your first message to the robot"
  - time: 30
    text: "Your second message to the robot"
```

## Fields

- **name**: Name of the benchmark (used for directory naming)
- **description**: Description of what the benchmark tests
- **goal**: The expected outcome or purpose of the benchmark
- **directive**: The directive to send to the robot
- **duration**: How long the benchmark should run (in seconds)
- **messages**: A list of messages to send at specific times
  - **time**: When to send the message (seconds after start)
  - **text**: The message content

## Running a Benchmark

To run a benchmark with a configuration file:

```bash
python benchmarks/benchmark_runner.py --config benchmarks/configs/your_config.yaml --trial 1

```

## Results

Benchmark results are stored in the `benchmarks/results/` directory, organized by benchmark name and trial number. 