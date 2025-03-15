#!/usr/bin/env python3
import json
import time
import argparse
import requests
import threading
import cv2
import yaml
import os
from datetime import datetime
from pathlib import Path
import websocket


class DirectiveBenchmark:
    """
    Benchmark runner for testing robot directives.
    Records video frames, chat messages, and performance metrics.
    """

    def __init__(
        self,
        config_file,
        trial_num=1,
        base_url="http://localhost:8000",
        frame_capture_interval=1.0,  # seconds between frame captures
    ):
        # Load configuration
        with open(config_file, "r") as f:
            self.config = yaml.safe_load(f)

        # Use values from config file
        self.directive = self.config.get("directive")
        self.duration = self.config.get("duration", 300)
        self.config_name = self.config.get(
            "name", os.path.basename(config_file).split(".")[0]
        )
        self.messages = self.config.get("messages", [])

        self.trial_num = trial_num
        self.base_url = base_url
        self.frame_capture_interval = frame_capture_interval
        self.config_file = config_file

        # Create directory structure
        output_base = self._sanitize_filename(self.config_name)

        # Store results in a dedicated results directory
        self.output_dir = Path(f"benchmarks/results/{output_base}/trial_{trial_num}")
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
        self.message_timers = []

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
            "config_name": self.config_name,
            "config_description": self.config.get("description", ""),
            "config_goal": self.config.get("goal", ""),
            "scheduled_messages": self.messages,
        }

        with open(self.output_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    def _save_chat_message(self, message):
        """Save a single chat message to the log file in real-time."""
        # Append to the chat log in memory
        self.chat_log.append(message)
        self.metrics["chat_messages"] += 1

        # Ensure the output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Append to the chat log file
        with open(self.output_dir / "chat_log.json", "w") as f:
            json.dump(self.chat_log, f, indent=2)

        # Update metrics file with the latest count
        self._save_metrics()

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
            result = response.json()
            status = result.get("status", "")

            if status == "directive_enqueued":
                return True
            elif status == "queue_full":
                print("Warning: Directive queue is full. Waiting and retrying...")
                # Wait a bit and try again
                time.sleep(5)
                retry_response = requests.post(
                    f"{self.base_url}/set_directive", json={"text": self.directive}
                )
                retry_result = retry_response.json()
                return retry_result.get("status") == "directive_enqueued"
            else:
                print(f"Unexpected status: {status}")
                return False
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
        """Monitor and record chat messages using WebSockets."""
        # Create a WebSocket URL
        base_url_no_http = self.base_url.replace("http://", "")
        user_params = "user_id=benchmark&email=benchmark@example.com"
        ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"
        print(f"Connecting to WebSocket: {ws_url}")

        # Get the start timestamp for filtering messages
        start_timestamp = self.metrics.get("start_timestamp", time.time())

        # Message handler for WebSocket
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "sender" in data and "text" in data:
                    # Check if the message has a timestamp
                    msg_time = data.get("timestamp", time.time())

                    # Only process messages that occurred after the test started
                    if msg_time >= start_timestamp:
                        print(f"Chat message: {data['sender']}: {data['text']}")
                        # Save message in real-time
                        self._save_chat_message(data)
                    else:
                        # Skip messages from before the test started
                        # print("Skipping message from before test start")
                        pass
            except Exception as e:
                print(f"Error processing message: {e}")

        # Error handler for WebSocket
        def on_error(ws, error):
            print(f"WebSocket error: {error}")

        # Connection close handler
        def on_close(ws, close_status_code, close_msg):
            print("WebSocket connection closed")

        # Connection open handler
        def on_open(ws):
            print("WebSocket connection established")

        # Create WebSocket connection
        try:
            # Create a WebSocket app
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )

            # Start WebSocket connection in a separate thread
            ws_thread = threading.Thread(target=ws.run_forever)
            ws_thread.daemon = True
            ws_thread.start()

            # Keep the thread running for the duration of the benchmark
            while self.running:
                time.sleep(1.0)

            # Close WebSocket connection
            ws.close()

        except Exception as e:
            print(f"Error setting up WebSocket: {e}")
            print("Chat messages will not be recorded in this benchmark")

            # Set a placeholder message
            placeholder_msg = {
                "sender": "system",
                "text": "Chat monitoring failed - WebSocket connection error",
                "timestamp": time.time(),
            }
            self._save_chat_message(placeholder_msg)

            # Keep the thread running for the duration of the benchmark
            while self.running:
                time.sleep(1.0)

    def _check_brain_status(self):
        """
        Check if the brain is running properly by monitoring initial chat messages.
        Returns True if the brain appears to be functioning, False otherwise.
        """
        print("Checking brain status...")

        # Create a WebSocket URL for monitoring brain status
        base_url_no_http = self.base_url.replace("http://", "")
        user_params = "user_id=brain_check&email=benchmark@example.com"
        ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"

        # Variables to track brain status
        brain_ok = False
        brain_error = False
        messages_received = 0
        check_complete = threading.Event()

        # Message handler for WebSocket
        def on_message(ws, message):
            nonlocal brain_ok, brain_error, messages_received
            try:
                data = json.loads(message)
                if "sender" in data and "text" in data:
                    messages_received += 1
                    text = data["text"].lower()

                    # Check for error messages
                    has_brain_error = (
                        "brain had a failure" in text or "brain malfunction" in text
                    )
                    if has_brain_error:
                        brain_error = True
                        print("Brain error detected in chat messages")
                        check_complete.set()

                    # If we've received several messages without errors,
                    # assume brain is OK
                    if messages_received >= 3 and not brain_error:
                        brain_ok = True
                        print("Brain appears to be functioning")
                        check_complete.set()
            except Exception as e:
                print(f"Error processing message during brain check: {e}")

        # Create WebSocket connection
        try:
            ws = websocket.WebSocketApp(
                ws_url,
                on_message=on_message,
                on_error=lambda ws, error: print(f"WebSocket error: {error}"),
                on_close=lambda ws, code, msg: print("WebSocket connection closed"),
                on_open=lambda ws: print("WebSocket connection established"),
            )

            # Start WebSocket connection in a separate thread
            ws_thread = threading.Thread(target=ws.run_forever)
            ws_thread.daemon = True
            ws_thread.start()

            # Wait for up to 15 seconds to determine brain status
            check_complete.wait(15)

            # Close WebSocket connection
            ws.close()

            if brain_error:
                print(
                    "WARNING: Brain errors detected. Consider resetting the brain "
                    "before continuing."
                )
                return False
            elif brain_ok:
                print("Brain check passed")
                return True
            else:
                print("Brain status check inconclusive")
                return True  # Continue anyway

        except Exception as e:
            print(f"Error checking brain status: {e}")
            return True  # Continue anyway if we can't check

    def _send_message(self, message_text):
        """Send a message to the robot using WebSockets."""
        try:
            # Create a WebSocket URL
            base_url_no_http = self.base_url.replace("http://", "")
            user_params = "user_id=benchmark&email=benchmark@example.com"
            ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"

            # Connect to WebSocket
            ws = websocket.create_connection(ws_url)

            # Send message - just send the text directly, not a JSON object
            ws.send(message_text)

            # Close connection
            ws.close()

            print(f"Message sent via WebSocket: '{message_text}'")
            return True
        except Exception as e:
            print(f"Error sending message via WebSocket: {e}")
            return False

    def _schedule_messages(self):
        """Schedule messages to be sent at specified times."""
        if not self.messages:
            return

        print(f"Scheduling {len(self.messages)} messages...")

        for message in self.messages:
            delay = message.get("time", 0)
            text = message.get("text", "")

            if delay >= 0 and text:
                # Create a timer to send the message after the specified delay
                timer = threading.Timer(delay, lambda msg=text: self._send_message(msg))
                timer.daemon = True
                self.message_timers.append(timer)
                print(f"Scheduled message at {delay}s: '{text}'")

        # Start all timers
        for timer in self.message_timers:
            timer.start()

    def run(self):
        """Run the benchmark test."""
        print(f"Starting benchmark for directive: '{self.directive}'")
        if self.config:
            print(f"Using configuration: '{self.config_name}'")
            if "description" in self.config:
                print(f"Description: {self.config.get('description')}")
            if "goal" in self.config:
                print(f"Goal: {self.config.get('goal')}")
        print(f"Output directory: {self.output_dir}")

        # Ensure the results directory exists
        Path("benchmarks/results").mkdir(parents=True, exist_ok=True)

        # Check if simulation is ready
        if not self._check_simulation_ready():
            print("Simulation is not ready. Please start the simulation first.")
            return False

        # Check if brain is functioning properly
        if not self._check_brain_status():
            print(
                "Brain check failed. You may want to reset the brain before continuing."
            )
            user_input = input("Continue anyway? (y/n): ")
            if user_input.lower() != "y":
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

        # Wait for the brain to process the directive
        print("Waiting for the brain to process the directive...")
        time.sleep(5)

        # Start data collection
        self.running = True
        self.metrics["start_time"] = datetime.now().isoformat()
        self.metrics["start_timestamp"] = (
            time.time()
        )  # Unix timestamp for filtering messages

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

        # Schedule messages if any are defined in the configuration
        self._schedule_messages()

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

            # Cancel any pending message timers
            for timer in self.message_timers:
                if timer.is_alive():
                    timer.cancel()

            # Record end time
            self.metrics["end_time"] = datetime.now().isoformat()

            # Save results - messages are saved in real-time
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

    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML configuration file",
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
        config_file=args.config,
        trial_num=args.trial,
        base_url=args.url,
        frame_capture_interval=args.interval,
    )

    benchmark.run()


if __name__ == "__main__":
    main()
