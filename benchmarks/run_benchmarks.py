#!/usr/bin/env python3
import argparse
import subprocess
import time


def run_benchmark(
    directive, duration=300, trial=1, base_url="http://localhost:8000", interval=1.0
):
    """Run a single benchmark with the given parameters."""
    cmd = [
        "./benchmarks/benchmark_runner.py",
        directive,
        "--duration",
        str(duration),
        "--trial",
        str(trial),
        "--url",
        base_url,
        "--interval",
        str(interval),
    ]

    print(f"\n{'='*80}")
    print(f"Running benchmark: '{directive}' (Trial {trial})")
    print(f"{'='*80}\n")

    subprocess.run(cmd)


def run_multiple_benchmarks(
    directives, trials=1, duration=300, base_url="http://localhost:8000", interval=1.0
):
    """Run multiple benchmarks with the given directives."""
    for directive in directives:
        for trial in range(1, trials + 1):
            run_benchmark(
                directive=directive,
                duration=duration,
                trial=trial,
                base_url=base_url,
                interval=interval,
            )

            # Add a short pause between benchmarks
            if trial < trials or directive != directives[-1]:
                print("\nWaiting 5 seconds before next benchmark...\n")
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Run multiple directive benchmarks")
    parser.add_argument(
        "--directives", nargs="+", required=True, help="List of directives to benchmark"
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials for each directive (default: 1)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Duration of each benchmark in seconds (default: 300)",
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

    run_multiple_benchmarks(
        directives=args.directives,
        trials=args.trials,
        duration=args.duration,
        base_url=args.url,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
