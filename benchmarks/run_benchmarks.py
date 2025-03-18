#!/usr/bin/env python3
import argparse
import subprocess
import time
import os
import sys
import yaml
import json


def run_benchmark(config_file, trial=1, base_url="http://localhost:8000", interval=1.0):
    """Run a single benchmark with the given config file."""
    cmd = [
        "python",
        "benchmarks/benchmark_runner.py",
        "--config",
        config_file,
        "--trial",
        str(trial),
        "--url",
        base_url,
        "--interval",
        str(interval),
    ]

    config_name = os.path.basename(config_file)
    print(f"\n{'='*80}")
    print(f"Running benchmark: '{config_name}' (Trial {trial})")
    print(f"{'='*80}\n")

    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Benchmark failed with exit code: {e.returncode}")
        return False
    except KeyboardInterrupt:
        print("Benchmark interrupted by user.")
        return False


def run_multiple_trials(
    config_file, num_trials, base_url, interval, start_trial=1, delay_between_trials=5
):
    """
    Run multiple trials of a benchmark in sequence
    """
    # Load the config file to check the number of initial parameter sets
    with open(config_file, "r") as f:
        config = yaml.safe_load(f)

    # Get the number of initial parameter sets
    initial_params = config.get("environment", {}).get("initial_parameters", [{}])
    num_param_sets = len(initial_params)

    # Check if the number of trials is a multiple of the number of parameter sets
    if num_param_sets > 1 and num_trials % num_param_sets != 0:
        print(
            f"\nWARNING: The number of trials ({num_trials}) is not a multiple of"
            f" the number of initial parameter sets ({num_param_sets})."
        )
        print("This means some parameter sets will be tested more times than others.")
        print(
            f"Consider using a multiple of {num_param_sets} trials"
            " for balanced testing."
        )

        response = input("Continue anyway? (y/n): ")
        if response.lower() != "y":
            print("Exiting.")
            return False

    print(f"Running {num_trials} trials of benchmark with config: {config_file}")
    if num_param_sets > 1:
        print(
            f"The config has {num_param_sets} different initial parameter sets"
            " that will be cycled through."
        )
    print(f"Starting from trial #{start_trial}")

    successful_trials = 0

    for trial_num in range(start_trial, start_trial + num_trials):
        # Calculate which parameter set this trial will use
        param_index = (trial_num - 1) % num_param_sets

        print(f"\n{'='*50}")
        if num_param_sets > 1:
            print(
                f"STARTING TRIAL #{trial_num} (using parameter set #{param_index + 1})"
            )
        else:
            print(f"STARTING TRIAL #{trial_num}")
        print(f"{'='*50}\n")

        success = run_benchmark(
            config_file=config_file,
            trial=trial_num,
            base_url=base_url,
            interval=interval,
        )

        if success:
            successful_trials += 1
            print(f"\nTrial #{trial_num} completed successfully")
        else:
            print(f"\nTrial #{trial_num} failed")
            response = input("Continue with remaining trials? (y/n): ")
            if response.lower() != "y":
                print("Aborting remaining trials.")
                break

        # Add a delay between trials to allow the system to stabilize
        if trial_num < start_trial + num_trials - 1:
            print(
                f"Waiting {delay_between_trials} seconds before starting next trial..."
            )
            time.sleep(delay_between_trials)

    print(f"\n{'='*50}")
    print(f"COMPLETED {successful_trials} OUT OF {num_trials} TRIALS")

    # Show summary of how many times each parameter set was tested
    if num_param_sets > 1:
        param_counts = {}
        for trial_num in range(start_trial, start_trial + num_trials):
            param_index = (trial_num - 1) % num_param_sets
            param_counts[param_index] = param_counts.get(param_index, 0) + 1

        print("\nSummary of parameter set usage:")
        for param_index, count in param_counts.items():
            print(f"  Parameter set #{param_index + 1}: {count} trials")

    print(f"{'='*50}")
    return successful_trials == num_trials


def run_benchmarks_from_config(
    config_file, base_url="http://localhost:8000", interval=1.0
):
    """
    Run multiple benchmarks as defined in a benchmarks configuration file
    """
    # Determine file type and load configuration
    if config_file.endswith(".json"):
        with open(config_file, "r") as f:
            benchmarks_config = json.load(f)
    elif config_file.endswith(".yaml") or config_file.endswith(".yml"):
        with open(config_file, "r") as f:
            benchmarks_config = yaml.safe_load(f)
    else:
        print(f"Unsupported config file format: {config_file}")
        return False

    # Extract benchmarks to run
    benchmarks = benchmarks_config.get("benchmarks", [])
    if not benchmarks:
        print("No benchmarks defined in configuration file.")
        return False

    print(f"Found {len(benchmarks)} benchmarks to run")

    total_benchmarks = len(benchmarks)
    successful_benchmarks = 0

    # Run each benchmark
    for i, benchmark in enumerate(benchmarks):
        print(f"\n{'#'*80}")
        print(f"BENCHMARK {i+1}/{total_benchmarks}: {benchmark.get('name', 'Unnamed')}")
        print(f"{'#'*80}\n")

        config_path = benchmark.get("config")
        trials = benchmark.get("trials", 1)
        start_trial = benchmark.get("start_trial", 1)
        delay = benchmark.get("delay", 5)

        if not config_path:
            print("Error: Missing 'config' path in benchmark definition.")
            continue

        # Expand ~ to user's home directory if present
        config_path = os.path.expanduser(config_path)

        # Check if config file exists
        if not os.path.exists(config_path):
            print(f"Error: Config file does not exist: {config_path}")
            continue

        # Run the benchmark with specified number of trials
        success = run_multiple_trials(
            config_file=config_path,
            num_trials=trials,
            base_url=base_url,
            interval=interval,
            start_trial=start_trial,
            delay_between_trials=delay,
        )

        if success:
            successful_benchmarks += 1

        # Add a delay between benchmarks
        if i < total_benchmarks - 1:
            print("\nWaiting 10 seconds before next benchmark...")
            time.sleep(10)

    print(f"\n{'#'*80}")
    print(f"COMPLETED {successful_benchmarks} OUT OF {total_benchmarks} BENCHMARKS")
    print(f"{'#'*80}")

    return successful_benchmarks == total_benchmarks


def main():
    parser = argparse.ArgumentParser(
        description="Run benchmarks for the robot simulation"
    )

    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # 'run' command for running a single benchmark with multiple trials
    run_parser = subparsers.add_parser(
        "run", help="Run a single benchmark with multiple trials"
    )
    run_parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML configuration file for the benchmark",
    )
    run_parser.add_argument(
        "--trials",
        type=int,
        default=3,
        help="Number of trials to run (default: 3)",
    )
    run_parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="Trial number to start from (default: 1)",
    )
    run_parser.add_argument(
        "--delay",
        type=int,
        default=5,
        help="Delay in seconds between trials (default: 5)",
    )

    # 'all' command for running all benchmarks defined in a config file
    all_parser = subparsers.add_parser(
        "all", help="Run all benchmarks defined in a config file"
    )
    all_parser.add_argument(
        "--config",
        default="benchmarks/benchmarks_config.json",
        help="Path to benchmarks configuration file"
        " (default: benchmarks/benchmarks_config.json)",
    )

    # Common arguments for both commands
    for subparser in [run_parser, all_parser]:
        subparser.add_argument(
            "--url",
            default="http://localhost:8000",
            help="Base URL for the API (default: http://localhost:8000)",
        )
        subparser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="Frame capture interval in seconds (default: 1.0)",
        )

    args = parser.parse_args()

    # Default to 'all' command if no command specified
    if not args.command:
        print("No command specified, running all benchmarks from default config...\n")
        if os.path.exists("benchmarks/benchmarks_config.json"):
            args.command = "all"
            args.config = "benchmarks/benchmarks_config.json"
            # Set default values for common arguments
            args.url = "http://localhost:8000"
            args.interval = 1.0
        else:
            parser.print_help()
            sys.exit(1)

    # Run the appropriate command
    if args.command == "run":
        run_multiple_trials(
            config_file=args.config,
            num_trials=args.trials,
            base_url=args.url,
            interval=args.interval,
            start_trial=args.start,
            delay_between_trials=args.delay,
        )
    elif args.command == "all":
        run_benchmarks_from_config(
            config_file=args.config,
            base_url=args.url,
            interval=args.interval,
        )


if __name__ == "__main__":
    main()
