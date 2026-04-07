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

        # Extract stopping reason
        stop_reason = "N/A"
        if metrics_data.get("early_success", {}).get("achieved"):
            stop_reason = metrics_data.get("early_success", {}).get(
                "reason", "Early success criterion met."
            )
        elif metrics_data.get("early_stop", {}).get("triggered"):
            stop_reason = metrics_data.get("early_stop", {}).get(
                "reason", "Early stop criterion met."
            )
        elif "success" in metrics_data and "reason" in metrics_data["success"]:
            stop_reason = metrics_data["success"]["reason"]
        else:
            stop_reason = "Benchmark timed out or ended without a specific reason."

        # The task name could be inferred from the path relative to the run_dir
        # e.g., {config_name}/trial_{trial_num}
        # Path(metrics_file_path).parent.parent.name could be the config_name
        task_name = Path(metrics_file_path).parent.parent.name
        trial_name = Path(metrics_file_path).parent.name

        return {
            "task_name": task_name,
            "trial_name": trial_name,
            "trial_path": str(Path(metrics_file_path).parent),
            "success": success,
            "duration_seconds": duration_seconds,
            "metrics_data": metrics_data,
            "stop_reason": stop_reason,
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
        return "<h1>Error: Provided run directory does not exist or is not a directory.</h1>"

    all_results = []
    for metrics_file in run_dir.rglob("**/metrics.json"):
        analysis_result = analyze_metrics_file(metrics_file)
        if analysis_result:
            all_results.append(analysis_result)

    if not all_results:
        return "<h1>No metrics.json files found in the specified directory.</h1>"

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

    overall_success_rate = (
        (total_successful_runs / total_runs * 100) if total_runs > 0 else 0
    )
    overall_avg_duration = (
        (total_duration_all_successful_runs / total_successful_runs)
        if total_successful_runs > 0
        else 0
    )

    # HTML report generation
    html = f"""
<html>
<head>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol"; line-height: 1.6; }}
    h1, h2, h3 {{ color: #1d2d50; }}
    .container {{ max-width: 800px; margin: 20px auto; padding: 20px; border: 1px solid #ddd; border-radius: 8px; background-color: #f9f9f9; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
    th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
    th {{ background-color: #f2f2f2; font-weight: bold; }}
    tr:nth-child(even) {{ background-color: #f9f9f9; }}
    .summary-box {{ background-color: #eaf2f8; border-left: 5px solid #3498db; padding: 15px; margin-bottom: 20px; }}
    .task-box {{ margin-bottom: 20px; }}
</style>
</head>
<body>
<div class="container">
    <h1>Benchmark Run Analysis Report</h1>
    <div class="summary-box">
        <h3>Overall Run Summary</h3>
        <p><strong>Run Directory:</strong> {run_dir.resolve()}</p>
        <p><strong>Total Benchmarks (trials):</strong> {total_runs}</p>
        <p><strong>Successful Benchmarks (trials):</strong> {total_successful_runs} ({{overall_success_rate:.2f}}%)</p>
        <p><strong>Avg Duration (all successful trials):</strong> {{overall_avg_duration:.2f}}s</p>
    </div>
    <h2>Per-Task Summary</h2>
    """

    for task_name, data in sorted(tasks_summary.items()):
        success_rate = (
            (data["successful_trials"] / data["total_trials"] * 100)
            if data["total_trials"] > 0
            else 0
        )
        avg_duration = (
            (data["total_duration_seconds"] / len(data["durations"]))
            if data["durations"]
            else 0
        )
        html += f"""
        <div class="task-box">
            <h3>Task: {task_name}</h3>
            <p><strong>Trials:</strong> {data['successful_trials']}/{data['total_trials']} successful ({{success_rate:.2f}}%)</p>
            <p><strong>Avg Duration (successful trials):</strong> {{avg_duration:.2f}}s</p>
        </div>
        """

    html += """
    <h2>Stop Reasons per Trial</h2>
    <table>
        <thead>
            <tr>
                <th>Task</th>
                <th>Trial</th>
                <th>Stop Reason</th>
            </tr>
        </thead>
        <tbody>
    """

    for result in sorted(all_results, key=lambda x: (x["task_name"], x["trial_name"])):
        html += f"""
            <tr>
                <td>{result['task_name']}</td>
                <td>{result['trial_name']}</td>
                <td>{result['stop_reason']}</td>
            </tr>
        """

    html += """
        </tbody>
    </table>
</div>
</body>
</html>
    """
    return html


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
