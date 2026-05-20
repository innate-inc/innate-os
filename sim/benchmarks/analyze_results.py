#!/usr/bin/env python3
import json
import argparse
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path
from datetime import datetime
import cv2
from PIL import Image, ImageDraw


class BenchmarkAnalyzer:
    """
    Analyzes benchmark results and generates reports.
    """

    def __init__(self, benchmark_dir):
        self.benchmark_dir = Path(benchmark_dir)
        if not self.benchmark_dir.exists():
            raise ValueError(f"Benchmark directory {benchmark_dir} does not exist")

    def list_directives(self):
        """List all directives that have been benchmarked."""
        directives = []
        for path in self.benchmark_dir.iterdir():
            if path.is_dir() and not path.name.startswith("."):
                directives.append(path.name)
        return directives

    def list_trials(self, directive):
        """List all trials for a given directive."""
        directive_path = self.benchmark_dir / directive
        trials = []
        for path in directive_path.iterdir():
            if path.is_dir() and path.name.startswith("trial_"):
                trials.append(path.name)
        return sorted(trials)

    def load_metadata(self, directive, trial):
        """Load metadata for a specific trial."""
        metadata_path = self.benchmark_dir / directive / trial / "metadata.json"
        if not metadata_path.exists():
            return None
        with open(metadata_path, "r") as f:
            return json.load(f)

    def load_metrics(self, directive, trial):
        """Load metrics for a specific trial."""
        metrics_path = self.benchmark_dir / directive / trial / "metrics.json"
        if not metrics_path.exists():
            return None
        with open(metrics_path, "r") as f:
            return json.load(f)

    def load_chat_log(self, directive, trial):
        """Load chat log for a specific trial."""
        chat_path = self.benchmark_dir / directive / trial / "chat_log.json"
        if not chat_path.exists():
            return []
        with open(chat_path, "r") as f:
            return json.load(f)

    def get_image_paths(self, directive, trial, camera_type="first_person"):
        """Get paths to all images for a specific trial and camera type."""
        image_dir = self.benchmark_dir / directive / trial / "images" / camera_type
        if not image_dir.exists():
            return []
        return sorted(
            [
                p
                for p in image_dir.iterdir()
                if p.suffix.lower() in [".jpg", ".jpeg", ".png"]
            ]
        )

    def create_summary_report(self, output_dir=None):
        """Create a summary report of all benchmarks."""
        if output_dir is None:
            output_dir = self.benchmark_dir / "reports"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        directives = self.list_directives()
        summary = {"timestamp": datetime.now().isoformat(), "directives": {}}

        # Collect data for each directive
        for directive in directives:
            trials = self.list_trials(directive)
            summary["directives"][directive] = {
                "trial_count": len(trials),
                "trials": {},
            }

            for trial in trials:
                metrics = self.load_metrics(directive, trial)
                metadata = self.load_metadata(directive, trial)

                if metrics and metadata:
                    # Calculate duration
                    if metrics["start_time"] and metrics["end_time"]:
                        start = datetime.fromisoformat(metrics["start_time"])
                        end = datetime.fromisoformat(metrics["end_time"])
                        duration = (end - start).total_seconds()
                    else:
                        duration = None

                    summary["directives"][directive]["trials"][trial] = {
                        "duration": duration,
                        "frames_captured": metrics["frames_captured"],
                        "chat_messages": metrics["chat_messages"],
                        "original_directive": metadata["directive"],
                    }

        # Save summary report
        with open(output_dir / "summary_report.json", "w") as f:
            json.dump(summary, f, indent=2)

        return summary

    def create_comparison_charts(self, output_dir=None):
        """Create charts comparing different directives."""
        if output_dir is None:
            output_dir = self.benchmark_dir / "reports"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        summary = self.create_summary_report(output_dir)
        directives = list(summary["directives"].keys())

        if not directives:
            print("No directives found to create charts")
            return

        # Prepare data for charts
        chat_counts = []
        first_person_frame_counts = []
        chase_frame_counts = []

        for directive in directives:
            directive_data = summary["directives"][directive]
            chat_sum = 0
            fp_frames_sum = 0
            chase_frames_sum = 0
            trial_count = 0

            for trial, trial_data in directive_data["trials"].items():
                chat_sum += trial_data["chat_messages"]
                fp_frames_sum += trial_data["frames_captured"]["first_person"]
                chase_frames_sum += trial_data["frames_captured"]["chase"]
                trial_count += 1

            if trial_count > 0:
                chat_counts.append(chat_sum / trial_count)
                first_person_frame_counts.append(fp_frames_sum / trial_count)
                chase_frame_counts.append(chase_frames_sum / trial_count)
            else:
                chat_counts.append(0)
                first_person_frame_counts.append(0)
                chase_frame_counts.append(0)

        # Create bar chart for chat messages
        plt.figure(figsize=(12, 6))
        plt.bar(directives, chat_counts)
        plt.title("Average Chat Messages per Directive")
        plt.xlabel("Directive")
        plt.ylabel("Average Message Count")
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        plt.savefig(output_dir / "chat_messages_comparison.png")
        plt.close()

        # Create bar chart for frames captured
        plt.figure(figsize=(12, 6))
        x = np.arange(len(directives))
        width = 0.35

        plt.bar(x - width / 2, first_person_frame_counts, width, label="First Person")
        plt.bar(x + width / 2, chase_frame_counts, width, label="Chase")

        plt.title("Average Frames Captured per Directive")
        plt.xlabel("Directive")
        plt.ylabel("Average Frame Count")
        plt.xticks(x, directives, rotation=45, ha="right")
        plt.legend()
        plt.tight_layout()
        plt.savefig(output_dir / "frames_comparison.png")
        plt.close()

    def create_timeline_video(self, directive, trial, output_dir=None, fps=5):
        """
        Create a timeline video showing first-person view, chase view,
         and chat messages.
        """
        if output_dir is None:
            output_dir = self.benchmark_dir / "reports"
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Get image paths
        first_person_paths = self.get_image_paths(directive, trial, "first_person")
        chase_paths = self.get_image_paths(directive, trial, "chase")

        if not first_person_paths or not chase_paths:
            print(f"Not enough images for {directive}/{trial} to create video")
            return

        # Load chat log
        chat_log = self.load_chat_log(directive, trial)

        # Determine video dimensions and initialize writer
        sample_img = cv2.imread(str(first_person_paths[0]))
        h, w = sample_img.shape[:2]

        # Create a larger canvas to fit both views and chat
        canvas_h = h * 2 + 100  # Extra space for chat
        canvas_w = w

        output_path = output_dir / f"{directive}_{trial}_timeline.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        video_writer = cv2.VideoWriter(
            str(output_path), fourcc, fps, (canvas_w, canvas_h)
        )

        # Process each frame
        for i, fp_path in enumerate(first_person_paths):
            # Create a blank canvas
            canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)

            # Add first-person view
            fp_img = cv2.imread(str(fp_path))
            canvas[:h, :w] = fp_img

            # Add chase view if available
            if i < len(chase_paths):
                chase_img = cv2.imread(str(chase_paths[i]))
                canvas[h : h * 2, :w] = chase_img

            # Extract timestamp from filename
            timestamp_str = fp_path.stem.split("_")[1]
            try:
                frame_timestamp = int(timestamp_str)

                # Add relevant chat messages
                chat_y = h * 2 + 20
                recent_messages = [
                    msg
                    for msg in chat_log
                    if "timestamp" in msg and msg["timestamp"] <= frame_timestamp
                ][
                    -5:
                ]  # Show last 5 messages

                # Convert to PIL for better text rendering
                pil_img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(pil_img)

                for j, msg in enumerate(recent_messages):
                    text = f"{msg.get('sender', 'Unknown')}: {msg.get('text', '')}"
                    draw.text((10, chat_y + j * 20), text, fill=(255, 255, 255))

                # Convert back to OpenCV format
                canvas = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

            except (ValueError, IndexError):
                pass

            # Add timestamp
            cv2.putText(
                canvas,
                f"Frame: {i}",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 255, 0),
                2,
            )

            # Write frame to video
            video_writer.write(canvas)

        video_writer.release()
        print(f"Created timeline video: {output_path}")
        return output_path


def main():
    parser = argparse.ArgumentParser(description="Analyze benchmark results")
    parser.add_argument(
        "--benchmark_dir",
        default="benchmarks",
        help="Directory containing benchmark results (default: benchmarks)",
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        help="Directory to save reports (default: benchmarks/reports)",
    )
    parser.add_argument(
        "--create_summary", action="store_true", help="Create summary report"
    )
    parser.add_argument(
        "--create_charts", action="store_true", help="Create comparison charts"
    )
    parser.add_argument(
        "--create_videos",
        action="store_true",
        help="Create timeline videos for all trials",
    )
    parser.add_argument(
        "--directive", help="Specific directive to analyze (for videos)"
    )
    parser.add_argument("--trial", help="Specific trial to analyze (for videos)")

    args = parser.parse_args()

    analyzer = BenchmarkAnalyzer(args.benchmark_dir)

    if args.create_summary or args.create_charts:
        analyzer.create_summary_report(args.output_dir)
        print("Created summary report")

    if args.create_charts:
        analyzer.create_comparison_charts(args.output_dir)
        print("Created comparison charts")

    if args.create_videos:
        if args.directive and args.trial:
            # Create video for specific directive and trial
            analyzer.create_timeline_video(args.directive, args.trial, args.output_dir)
        else:
            # Create videos for all directives and trials
            directives = analyzer.list_directives()
            for directive in directives:
                trials = analyzer.list_trials(directive)
                for trial in trials:
                    analyzer.create_timeline_video(directive, trial, args.output_dir)


if __name__ == "__main__":
    main()
