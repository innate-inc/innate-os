#!/usr/bin/env python3
import json
import time
import argparse
import requests
import threading
import cv2
from datetime import datetime
from pathlib import Path


class DirectiveBenchmark:
    """
    Benchmark runner for testing robot directives.
    Records video frames, chat messages, and performance metrics.
    """

    def __init__(
        self,
        directive,
        duration=300,  # 5 minutes in seconds
        trial_num=1,
        base_url="http://localhost:8000",
        frame_capture_interval=1.0,  # seconds between frame captures
    ):
        self.directive = directive
        self.duration = duration
        self.trial_num = trial_num
        self.base_url = base_url
        self.frame_capture_interval = frame_capture_interval

        # Create directory structure
        directive_safe = self._sanitize_filename(directive)
        self.output_dir = Path(f"benchmarks/{directive_safe}/trial_{trial_num}")
        self.images_dir = self.output_dir / "images"
        self.first_person_dir = self.images_dir / "first_person"
        self.chase_dir = self.images_dir / "chase"

        # Create directories
        self.first_person_dir.mkdir(parents=True, exist_ok=True)
        self.chase_dir.mkdir(parents=True, exist_ok=True)

        # Initialize data structures
        self.chat_log = []
        self.metrics = {
            "start_time": None,
            "end_time": None,
            "frames_captured": {"first_person": 0, "chase": 0},
            "chat_messages": 0,
        }

        # Control flags
        self.running = False
        self.threads = []

    def _sanitize_filename(self, name):
        """Convert a string to a safe filename."""
        return "".join(c if c.isalnum() or c in ["-", "_"] else "_" for c in name)

    def _save_metadata(self):
        """Save test metadata to a JSON file."""
        metadata = {
            "directive": self.directive,
            "duration": self.duration,
            "trial_num": self.trial_num,
            "timestamp": datetime.now().isoformat(),
            "base_url": self.base_url,
        }

        with open(self.output_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    def _save_chat_log(self):
        """Save the chat log to a JSON file."""
        with open(self.output_dir / "chat_log.json", "w") as f:
            json.dump(self.chat_log, f, indent=2)

    def _save_metrics(self):
        """Save performance metrics to a JSON file."""
        with open(self.output_dir / "metrics.json", "w") as f:
            json.dump(self.metrics, f, indent=2)

    def _check_simulation_ready(self):
        """Check if the simulation is ready."""
        try:
            response = requests.get(f"{self.base_url}/video_feeds_ready")
            data = response.json()
            return data.get("ready", False)
        except Exception as e:
            print(f"Error checking simulation status: {e}")
            return False

    def _reset_robot(self):
        """Reset the robot to its starting position."""
        try:
            response = requests.post(f"{self.base_url}/reset_robot")
            return response.json().get("status") == "reset_enqueued"
        except Exception as e:
            print(f"Error resetting robot: {e}")
            return False

    def _send_directive(self):
        """Send the directive to the robot."""
        try:
            response = requests.post(
                f"{self.base_url}/set_directive", json={"text": self.directive}
            )
            return response.json().get("status") == "directive_enqueued"
        except Exception as e:
            print(f"Error sending directive: {e}")
            return False

    def _capture_frames(self, camera_type):
        """Continuously capture frames from the specified camera."""
        url = (
            f"{self.base_url}/video_feed"
            if camera_type == "first_person"
            else f"{self.base_url}/video_feed_chase"
        )
        output_dir = (
            self.first_person_dir if camera_type == "first_person" else self.chase_dir
        )

        # Use OpenCV to capture frames from the MJPEG stream
        cap = cv2.VideoCapture(url)
        frame_count = 0
        last_capture_time = time.time()

        while self.running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            current_time = time.time()
            # Save frame at specified interval
            if current_time - last_capture_time >= self.frame_capture_interval:
                timestamp = int(current_time)
                filename = f"{camera_type}_{timestamp}_{frame_count:04d}.jpg"
                cv2.imwrite(str(output_dir / filename), frame)
                frame_count += 1
                self.metrics["frames_captured"][camera_type] += 1
                last_capture_time = current_time

            time.sleep(0.01)  # Small sleep to prevent CPU hogging

        cap.release()

    def _monitor_chat(self):
        """Monitor and record chat messages."""
        # This is a placeholder - in a real implementation, you would
        # connect to your chat API and record messages
        last_check_time = time.time()
        check_interval = 1.0  # Check for new messages every second

        while self.running:
            current_time = time.time()
            if current_time - last_check_time >= check_interval:
                try:
                    # Get recent chat messages
                    response = requests.get(f"{self.base_url}/chat/messages")
                    messages = response.json()

                    # Add new messages to our log
                    for msg in messages:
                        if msg not in self.chat_log:
                            self.chat_log.append(msg)
                            self.metrics["chat_messages"] += 1

                    last_check_time = current_time
                except Exception as e:
                    print(f"Error monitoring chat: {e}")

            time.sleep(0.1)

    def run(self):
        """Run the benchmark test."""
        print(f"Starting benchmark for directive: '{self.directive}'")
        print(f"Output directory: {self.output_dir}")

        # Check if simulation is ready
        if not self._check_simulation_ready():
            print("Simulation is not ready. Please start the simulation first.")
            return False

        # Save metadata
        self._save_metadata()

        # Reset the robot
        print("Resetting robot...")
        if not self._reset_robot():
            print("Failed to reset robot.")
            return False

        # Wait for reset to complete
        time.sleep(2)

        # Send directive
        print(f"Sending directive: '{self.directive}'")
        if not self._send_directive():
            print("Failed to send directive.")
            return False

        # Start data collection
        self.running = True
        self.metrics["start_time"] = datetime.now().isoformat()

        # Start threads for frame capture and chat monitoring
        first_person_thread = threading.Thread(
            target=self._capture_frames, args=("first_person",)
        )
        chase_thread = threading.Thread(target=self._capture_frames, args=("chase",))
        chat_thread = threading.Thread(target=self._monitor_chat)

        self.threads = [first_person_thread, chase_thread, chat_thread]
        for thread in self.threads:
            thread.daemon = True
            thread.start()

        print(f"Benchmark running for {self.duration} seconds...")

        # Run for specified duration
        try:
            time.sleep(self.duration)
        except KeyboardInterrupt:
            print("Benchmark interrupted by user.")
        finally:
            # Stop all threads
            self.running = False
            for thread in self.threads:
                thread.join(timeout=2.0)

            # Record end time
            self.metrics["end_time"] = datetime.now().isoformat()

            # Save results
            self._save_chat_log()
            self._save_metrics()

            print("Benchmark completed.")
            print(
                f"Captured {self.metrics['frames_captured']['first_person']} "
                f"first-person frames"
            )
            print(f"Captured {self.metrics['frames_captured']['chase']} chase frames")
            print(f"Recorded {self.metrics['chat_messages']} chat messages")

        return True


def main():
    parser = argparse.ArgumentParser(
        description="Run directive benchmarks for robot simulation"
    )
    parser.add_argument("directive", help="The directive to send to the robot")
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Test duration in seconds (default: 300)",
    )
    parser.add_argument(
        "--trial", type=int, default=1, help="Trial number (default: 1)"
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

    benchmark = DirectiveBenchmark(
        directive=args.directive,
        duration=args.duration,
        trial_num=args.trial,
        base_url=args.url,
        frame_capture_interval=args.interval,
    )

    benchmark.run()


if __name__ == "__main__":
    main()
