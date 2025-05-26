#!/usr/bin/env python3
import argparse
import subprocess
import time
import os
import sys
import yaml
import json
import requests
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv
from datetime import datetime
from pathlib import Path

from analyze_full_run import generate_report


def send_email_summary(
    subject,
    body,
    to_email,
    from_email,
    smtp_server,
    smtp_port,
    smtp_user,
    smtp_password,
):
    """Sends an email with the benchmark summary."""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Use TLS encryption
            server.login(smtp_user, smtp_password)
            server.sendmail(from_email, to_email, msg.as_string())
        print(f"Email summary sent successfully to {to_email}")
    except Exception as e:
        print(f"Error sending email: {e}")


def run_benchmark(
    config_file,
    trial=1,
    base_url="http://localhost:8000",
    interval=1.0,
    variant=None,
    output_dir_base=None,
):
    """Run a single benchmark with the given config file."""
    cmd = [
        "python",
        "benchmark_runner.py",
        "--config",
        config_file,
        "--trial",
        str(trial),
        "--url",
        base_url,
        "--interval",
        str(interval),
    ]

    # Add variant parameter if provided
    if variant:
        cmd.extend(["--variant", variant])

    # Add output directory parameter if provided
    if output_dir_base:
        cmd.extend(["--output-dir", output_dir_base])

    config_name = os.path.basename(config_file)
    print(f"\n{'='*80}")
    print(f"Running benchmark: '{config_name}' (Trial {trial})")
    if variant:
        print(f"Using variant: {variant}")
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
    config_file,
    num_trials,
    base_url,
    interval,
    start_trial=1,
    delay_between_trials=5,
    variant=None,
    output_dir_base=None,
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
    if variant:
        print(f"Using variant: {variant}")
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
            variant=variant,
            output_dir_base=output_dir_base,
        )

        if success:
            successful_trials += 1
            print(f"\nTrial #{trial_num} completed successfully")
        else:
            print(f"\nTrial #{trial_num} failed")

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
        return False, [], None  # Return None for main_output_directory path

    # Extract benchmarks to run
    benchmarks = benchmarks_config.get("benchmarks", [])
    if not benchmarks:
        print("No benchmarks defined in configuration file.")
        return False, [], None  # Return None for main_output_directory path

    print(f"Found {len(benchmarks)} benchmarks to run")

    total_benchmarks = len(benchmarks)
    successful_benchmarks = 0
    benchmark_results_summary = []  # To store summary of each benchmark

    # Create a timestamped base directory for all benchmarks in this run
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    main_output_directory = Path("benchmarks/runs") / timestamp_str
    main_output_directory.mkdir(parents=True, exist_ok=True)
    print(f"Saving all benchmark data for this run to: {main_output_directory}")
    main_output_directory_str = str(main_output_directory)  # Store as string for return

    # Run each benchmark
    for i, benchmark in enumerate(benchmarks):
        benchmark_name = benchmark.get("name", f"Unnamed Benchmark {i+1}")
        print(f"\n{'#'*80}")
        print(f"BENCHMARK {i+1}/{total_benchmarks}: {benchmark_name}")
        print(f"{'#'*80}\n")

        config_path = benchmark.get("config")
        trials = benchmark.get("trials", 1)
        start_trial = benchmark.get("start_trial", 1)
        delay = benchmark.get("delay", 5)
        variants = benchmark.get("variants", None)

        if not config_path:
            print("Error: Missing 'config' path in benchmark definition.")
            continue

        # Expand ~ to user's home directory if present
        config_path = os.path.expanduser(config_path)

        # Check if config file exists
        if not os.path.exists(config_path):
            print(f"Error: Config file does not exist: {config_path}")
            continue

        # If variants is specified, run the benchmark for each variant
        if variants and len(variants) > 0:
            variants_str = ", ".join(variants)
            print(f"Testing with {len(variants)} variants: {variants_str}")
            total_trials = trials * len(variants)
            print(
                f"Will run a total of {total_trials} trials "
                f"({trials} trials per variant)"
            )

            variant_successful_trials = 0
            current_trial = start_trial

            for variant_idx, variant in enumerate(variants):
                print(f"\n{'='*60}")
                print(
                    f"RUNNING WITH VARIANT: {variant} "
                    f"({variant_idx+1}/{len(variants)})"
                )
                print(f"{'='*60}\n")

                # If not the first variant, we need to wait a bit
                # to ensure system stability before switching variants
                if variant_idx > 0:
                    wait_msg = "Waiting 10 seconds before switching to next variant..."
                    print(wait_msg)
                    time.sleep(10)

                # Run the benchmark with specified number of trials for this variant
                success = run_multiple_trials(
                    config_file=config_path,
                    num_trials=trials,
                    base_url=base_url,
                    interval=interval,
                    start_trial=current_trial,
                    delay_between_trials=delay,
                    variant=variant,
                    output_dir_base=str(main_output_directory),
                )

                if success:
                    variant_successful_trials += trials

                # Update the current trial for the next variant
                current_trial += trials

            # Check if all trials across all variants were successful
            if variant_successful_trials == total_trials:
                successful_benchmarks += 1
                benchmark_results_summary.append(
                    f"{benchmark_name}: All {total_trials} trials successful (var)."
                )
            else:
                benchmark_results_summary.append(
                    f"{benchmark_name}: {variant_successful_trials}/{total_trials} "
                    f"trials (across variants) successful."
                )

            print(f"\n{'='*60}")
            print(
                f"COMPLETED {variant_successful_trials} OUT OF {total_trials} "
                f"TRIALS ACROSS ALL VARIANTS"
            )
            print(f"{'='*60}")

        else:
            # Run the benchmark normally with specified number of trials
            success = run_multiple_trials(
                config_file=config_path,
                num_trials=trials,
                base_url=base_url,
                interval=interval,
                start_trial=start_trial,
                delay_between_trials=delay,
                variant=None,
                output_dir_base=str(main_output_directory),
            )

            if success:
                successful_benchmarks += 1
                benchmark_results_summary.append(
                    f"{benchmark_name}: All {trials} trials successful."
                )
            else:
                benchmark_results_summary.append(
                    f"{benchmark_name}: Failed after {trials} trials "
                    f"(or fewer if interrupted)."
                )

        # Add a delay between benchmarks
        if i < total_benchmarks - 1:
            print("\nWaiting 10 seconds before next benchmark...")
            time.sleep(10)

    print(f"\n{'#'*80}")
    print(f"COMPLETED {successful_benchmarks} OUT OF {total_benchmarks} BENCHMARKS")
    print(f"{'#'*80}")

    return (
        successful_benchmarks == total_benchmarks,
        benchmark_results_summary,
        main_output_directory_str,
    )


def stop_simulator(base_url="http://localhost:8000"):
    """Stop the simulator by sending a shutdown request to the API."""
    try:
        print("Sending shutdown request to simulator...")
        headers = {}
        headers["Authorization"] = "Bearer NOT_NEEDED"
        response = requests.post(f"{base_url}/shutdown", headers=headers)
        if response.status_code == 200:
            print("Successfully requested simulator shutdown.")
            return True
        else:
            print(f"Failed to stop simulator. Status code: {response.status_code}")
            return False
    except Exception as e:
        print(f"Error stopping simulator: {e}")
        return False


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
        default="benchmarks_config.json",
        help="Path to benchmarks configuration file"
        " (default: benchmarks_config.json)",
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
        subparser.add_argument(
            "--stop-simulator",
            action="store_true",
            help="Stop the simulator after benchmarks are completed",
        )
        # Email arguments - common to both, but only used by 'all'
        subparser.add_argument(
            "--send-email",
            action="store_true",
            help=(
                "Send an email summary after benchmarks are completed "
                "(only for 'all' command)"
            ),
        )
        subparser.add_argument(
            "--to-email", help="Email address to send the summary to"
        )
        subparser.add_argument(
            "--from-email", help="Email address to send the summary from"
        )
        subparser.add_argument("--smtp-server", help="SMTP server address")
        subparser.add_argument(
            "--smtp-port",
            type=int,
            default=587,
            help="SMTP server port (default: 587 for TLS)",
        )
        subparser.add_argument("--smtp-user", help="SMTP username")
        subparser.add_argument("--smtp-password", help="SMTP password")

    args = parser.parse_args()

    # Load .env file
    load_dotenv()

    # Default to 'all' command if no command specified
    if not args.command:
        print("No command specified, running all benchmarks from default config...\n")
        if os.path.exists("benchmarks_config.json"):
            args.command = "all"
            args.config = "benchmarks_config.json"
            # Set default values for common arguments
            args.url = "http://localhost:8000"
            args.interval = 1.0
            args.stop_simulator = True
        else:
            parser.print_help()
            sys.exit(1)

    # Run the appropriate command
    if args.command == "run":
        if args.send_email:
            print(
                "Warning: Email sending is only supported with the 'all' command. "
                "Email will not be sent."
            )

        success = run_multiple_trials(
            config_file=args.config,
            num_trials=args.trials,
            base_url=args.url,
            interval=args.interval,
            start_trial=args.start,
            delay_between_trials=args.delay,
            variant=None,
            output_dir_base=None,
        )
        # Stop simulator if requested
        if args.stop_simulator:
            stop_simulator(args.url)

        if success:
            print(f"Benchmark {args.config} ({args.trials} trials) successful.")
        else:
            print(f"Benchmark {args.config} failed/interrupted.")

    elif args.command == "all":
        overall_success, results_summary, main_run_output_dir = (
            run_benchmarks_from_config(
                config_file=args.config,
                base_url=args.url,
                interval=args.interval,
            )
        )
        # Stop simulator if requested
        if args.stop_simulator:
            stop_simulator(args.url)

        # Print overall status
        if overall_success:
            print("All benchmarks completed successfully.")
        else:
            print("Some benchmarks failed.")

        if args.send_email:
            email_subject = "Benchmark Run Summary"
            analysis_report = (
                "Analysis script failed, no data found, or error in generation."
            )

            if main_run_output_dir and Path(main_run_output_dir).is_dir():
                try:
                    print(f"Generating analysis report for: {main_run_output_dir}")
                    analysis_report = generate_report(str(main_run_output_dir))
                    print("Analysis report generated successfully.")
                except Exception as e:
                    print(f"Error generating analysis report: {e}")
                    analysis_report = (
                        f"Failed to generate analysis report: {str(e)[:300]}"
                    )
            elif main_run_output_dir:
                analysis_report = (
                    f"Run output dir for analysis invalid: {main_run_output_dir}"
                )
            else:
                analysis_report = (
                    "Could not determine run output directory for analysis."
                )

            email_body = "Benchmark run completed.\n\nRaw Summary:\n"
            email_body += "\n".join(results_summary)
            email_body += "\n\n" + "=" * 40
            email_body += "\n\nDetailed Analysis Report:\n"
            email_body += "=" * 40
            email_body += "\n"
            email_body += analysis_report

            # Fallback to .env for email config
            to_email = args.to_email or os.getenv("TO_EMAIL")
            from_email = args.from_email or os.getenv("FROM_EMAIL")
            smtp_server = args.smtp_server or os.getenv("SMTP_SERVER")
            smtp_port_env = os.getenv("SMTP_PORT")
            if args.smtp_port:
                smtp_port = args.smtp_port
            elif smtp_port_env:
                smtp_port = int(smtp_port_env)
            else:
                smtp_port = 587
            smtp_user = args.smtp_user or os.getenv("SMTP_USER")
            smtp_password = args.smtp_password or os.getenv("SMTP_PASSWORD")

            required_params = [
                to_email,
                from_email,
                smtp_server,
                smtp_user,
                smtp_password,
            ]
            if not all(required_params):
                print("Error: Missing email parameters (CLI or .env). Email not sent.")
                print(
                    "Required: TO_EMAIL, FROM_EMAIL, SMTP_SERVER, "
                    + "SMTP_USER, SMTP_PASSWORD"
                )
            else:
                send_email_summary(
                    subject=email_subject,
                    body=email_body,
                    to_email=to_email,
                    from_email=from_email,
                    smtp_server=smtp_server,
                    smtp_port=smtp_port,
                    smtp_user=smtp_user,
                    smtp_password=smtp_password,
                )


if __name__ == "__main__":
    main()
