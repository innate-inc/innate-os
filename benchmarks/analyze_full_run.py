import argparse
import json

# import os # Unused, remove
from pathlib import Path
from datetime import datetime


def analyze_metrics_file(metrics_file_path):
    """Analyzes a single metrics.json file."""
    try:
        with open(metrics_file_path, "r") as f:
            metrics_data = json.load(f)

        success = metrics_data.get("success", {}).get("success", False)
        start_time_str = metrics_data.get("start_time")
        end_time_str = metrics_data.get("end_time")

        duration_seconds = None
        if start_time_str and end_time_str:
            try:
                start_time = datetime.fromisoformat(start_time_str)
                end_time = datetime.fromisoformat(end_time_str)
                duration_seconds = (end_time - start_time).total_seconds()
            except ValueError as e:
                print(f"Error parsing timestamps in {metrics_file_path}: {e}")

        # The task name could be inferred from the path relative to the run_dir
        # e.g., {config_name}/trial_{trial_num}
        # Path(metrics_file_path).parent.parent.name could be the config_name
        task_name = Path(metrics_file_path).parent.parent.name

        return {
            "task_name": task_name,
            "trial_path": str(Path(metrics_file_path).parent),
            "success": success,
            "duration_seconds": duration_seconds,
            "metrics_data": metrics_data,
        }
    except Exception as e:
        print(f"Error processing metrics file {metrics_file_path}: {e}")
        return None


def generate_report(run_directory_path):
    """
    Analyzes all metrics.json files in a run directory and generates a report.
    """
    run_dir = Path(run_directory_path)
    if not run_dir.is_dir():
        return "Error: Provided run directory does not exist or is not a directory."

    all_results = []
    for metrics_file in run_dir.rglob("**/metrics.json"):
        analysis_result = analyze_metrics_file(metrics_file)
        if analysis_result:
            all_results.append(analysis_result)

    if not all_results:
        return "No metrics.json files found in the specified directory."

    # Aggregate results by task name
    tasks_summary = {}
    total_successful_runs = 0
    total_runs = len(all_results)
    total_duration_all_successful_runs = 0

    for result in all_results:
        task_name = result["task_name"]
        if task_name not in tasks_summary:
            tasks_summary[task_name] = {
                "successful_trials": 0,
                "total_trials": 0,
                "total_duration_seconds": 0,
                "durations": [],  # To calculate average later
            }

        tasks_summary[task_name]["total_trials"] += 1
        if result["success"]:
            tasks_summary[task_name]["successful_trials"] += 1
            total_successful_runs += 1
            if result["duration_seconds"] is not None:
                tasks_summary[task_name]["total_duration_seconds"] += result[
                    "duration_seconds"
                ]
                tasks_summary[task_name]["durations"].append(result["duration_seconds"])
                total_duration_all_successful_runs += result["duration_seconds"]

    report_lines = [
        "Benchmark Run Analysis Report",
        "=" * 30,
        f"Run Directory: {run_dir.resolve()}",
        "",
    ]

    report_lines.append("Per-Task Summary:")
    report_lines.append("-" * 30)

    for task_name, data in tasks_summary.items():
        report_lines.append(f"Task: {task_name}")
        success_rate = (
            (data["successful_trials"] / data["total_trials"] * 100)
            if data["total_trials"] > 0
            else 0
        )
        trials_summary = (
            f"  Trials: {data['successful_trials']}/{data['total_trials']} "
            f"successful ({success_rate:.2f}%)"
        )
        report_lines.append(trials_summary)

        avg_duration = (
            (data["total_duration_seconds"] / len(data["durations"]))
            if data["durations"]
            else None
        )
        if avg_duration is not None:
            report_lines.append(f"  Avg Duration (ok): {avg_duration:.2f}s")
        else:
            report_lines.append(
                "  Avg Duration (ok): N/A (no successful runs w/ duration)"
            )
        report_lines.append("")

    report_lines.append("Overall Run Summary:")
    report_lines.append("-" * 30)
    overall_success_rate = (
        (total_successful_runs / total_runs * 100) if total_runs > 0 else 0
    )
    report_lines.append(f"Total Benchmarks (trials): {total_runs}")
    overall_summary_text = (
        f"Successful Benchmarks (trials): {total_successful_runs} "
        f"({overall_success_rate:.2f}%)"
    )
    report_lines.append(overall_summary_text)

    overall_avg_duration = (
        (total_duration_all_successful_runs / total_successful_runs)
        if total_successful_runs > 0
        else None
    )
    if overall_avg_duration is not None:
        report_lines.append(
            f"Avg Duration (all successful trials): {overall_avg_duration:.2f}s"
        )
    else:
        report_lines.append("Avg Duration (all successful trials): N/A")

    report_lines.append("=" * 30)
    return "\n".join(report_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a benchmark run directory and generate a report."
    )
    parser.add_argument(
        "run_directory",
        type=str,
        help=(
            "Path to the main run directory " "(e.g., benchmarks/runs/YYYYMMDD_HHMMSS)"
        ),
    )
    args = parser.parse_args()

    report = generate_report(args.run_directory)
    print(report)


if __name__ == "__main__":
    main()
