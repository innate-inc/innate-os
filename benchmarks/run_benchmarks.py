#!/usr/bin/env python3
import argparse
import subprocess
import time
import glob
import os
from pathlib import Path


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

    subprocess.run(cmd)


def run_multiple_benchmarks(
    config_files, trials=1, base_url="http://localhost:8000", interval=1.0
):
    """Run multiple benchmarks with the given configuration files."""
    for config_file in config_files:
        for trial in range(1, trials + 1):
            run_benchmark(
                config_file=config_file,
                trial=trial,
                base_url=base_url,
                interval=interval,
            )

            # Add a short pause between benchmarks
            if trial < trials or config_file != config_files[-1]:
                print("\nWaiting 5 seconds before next benchmark...\n")
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(
        description="Run multiple benchmark configurations"
    )

    # Config specification options - use one of these
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--configs", nargs="+", help="List of config files to run")
    group.add_argument(
        "--category", help="Run all configs in a specific category folder"
    )
    group.add_argument(
        "--all", action="store_true", help="Run all available benchmark configs"
    )

    # Other parameters
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials for each config (default: 1)",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL for the API (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Frame capture interval in seconds (default: 1.0)",
    )

    args = parser.parse_args()

    # Determine which config files to run
    config_files = []

    if args.configs:
        # Specific config files provided
        config_files = args.configs
    elif args.category:
        # Run all configs in a specific category folder
        category_path = Path(f"benchmarks/configs/{args.category}")
        if not category_path.exists():
            print(f"Error: Category folder '{args.category}' does not exist")
            return

        config_files = glob.glob(str(category_path / "*.yaml"))

        if not config_files:
            print(f"Error: No config files found in category '{args.category}'")
            return
    elif args.all:
        # Run all available configs
        config_files = glob.glob("benchmarks/configs/**/*.yaml", recursive=True)

        if not config_files:
            print("Error: No config files found")
            return

    # Sort config files for consistent ordering
    config_files.sort()

    print(f"Found {len(config_files)} benchmark configurations to run")
    for i, config in enumerate(config_files):
        print(f"{i+1}. {config}")
    print()

    run_multiple_benchmarks(
        config_files=config_files,
        trials=args.trials,
        base_url=args.url,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
