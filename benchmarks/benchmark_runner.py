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
from src.check_validation import (
    validate_checks,
    evaluate_stop_criterion,
    evaluate_final_success,
)
from src.websocket_manager import WebSocketManager


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
        variant=None,  # Gemini variant to use
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
        # Store the variant to use
        self.variant = variant

        # Handle the new format messages (backward compatibility for old format)
        self.messages = []
        if "messages" in self.config:
            for msg in self.config.get("messages", []):
                # Check if this is in the new format with trigger_type
                if "trigger_type" in msg:
                    # Convert to internal message format
                    if msg["trigger_type"] == "time":
                        self.messages.append(
                            {
                                "time": msg["time"],
                                "text": msg["text"],
                                "trigger_type": "time",
                            }
                        )
                    elif msg["trigger_type"] == "check":
                        self.messages.append(
                            {
                                "check_id": msg["check_id"],
                                "delay": msg.get("delay", 0),
                                "text": msg["text"],
                                "trigger_type": "check",
                            }
                        )
                # Legacy format support
                elif "time" in msg and "text" in msg:
                    self.messages.append(
                        {
                            "time": msg["time"],
                            "text": msg["text"],
                            "trigger_type": "time",
                        }
                    )

        # Environment settings (new format)
        self.environment = self.config.get("environment", {})
        self.env_name = self.environment.get("name", "default")
        self.initial_parameters = self.environment.get("initial_parameters", [{}])

        # Get the parameters for this trial (cycle through them)
        if self.initial_parameters:
            param_index = (trial_num - 1) % len(self.initial_parameters)
            self.current_parameters = self.initial_parameters[param_index]
        else:
            self.current_parameters = {}

        # Stop criterion (new format)
        self.expectations = self.config.get("expectations", {})
        self.early_stop_criterion = self.expectations.get("early_stop_criterion", None)

        # Whether to use frames for evaluation (default: True)
        self.use_frames = self.expectations.get("use_frames", True)

        # Track check status for message triggers
        self.check_status = {}
        self.check_completion_times = {}

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
        self.position_history = []  # Track robot positions throughout the run

        # Only store essential metrics in the metrics object
        self.metrics = {
            "start_time": None,
            "end_time": None,
            "frames_captured": {"first_person": 0, "chase": 0},
            "chat_messages": 0,
        }

        # We'll use this to track timestamps for position history
        self.position_timestamps = []

        # Control flags
        self.running = False
        self.threads = []
        self.message_timers = []

        # Initialize WebSocket manager
        self.websocket_manager = WebSocketManager(
            base_url=self.base_url, save_chat_message_callback=self._save_chat_message
        )

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
            "environment": self.env_name,
            "initial_parameters": self.current_parameters,
            "scheduled_messages": self.messages,
            "variant": self.variant,
        }

        with open(self.output_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

    def _save_chat_message(self, message):
        """Save a single chat message to the log file in real-time."""
        # Calculate time since benchmark start
        if "timestamp" in message and self.metrics.get("start_timestamp"):
            time_since_start = message["timestamp"] - self.metrics["start_timestamp"]
            # Add time_since_start to the message
            message["time_since_start"] = round(time_since_start, 2)

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

    def _save_position_history(self):
        """Save robot position history to a separate JSON file."""
        position_data = []

        # Combine positions with timestamps
        for i, position in enumerate(self.position_history):
            # Only add timestamp if available
            if i < len(self.position_timestamps):
                timestamp = self.position_timestamps[i]
                position_data.append(
                    {"timestamp": timestamp, "position": [position[0], position[1]]}
                )
            else:
                # Fallback for positions without timestamps (shouldn't happen)
                position_data.append({"position": [position[0], position[1]]})

        with open(self.output_dir / "position_history.json", "w") as f:
            json.dump(position_data, f, indent=2)

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
        """
        Reset the robot to its starting position.
        If init_memory is specified in the config, it will be used to initialize robot memory.
        If robot_position and robot_orientation are specified in current_parameters,
        they will be used to set the robot's initial pose.
        """
        try:
            # Check if init_memory is present in the config
            memory_state = self.config.get("init_memory")

            # Create the request data object
            data = {}

            # Add memory_state if available
            if memory_state:
                print(f"Resetting robot with memory state: {memory_state}")
                data["memory_state"] = memory_state

            # Add position and orientation if available in current parameters
            if self.current_parameters:
                if (
                    "robot_position" in self.current_parameters
                    and "robot_orientation" in self.current_parameters
                ):
                    position = self.current_parameters["robot_position"]
                    orientation = self.current_parameters["robot_orientation"]
                    print(
                        f"Setting robot position to: {position} and orientation to: {orientation}"
                    )
                    data["position"] = position
                    data["orientation"] = orientation
                else:
                    print(
                        "No position and orientation specified together in current parameters"
                    )

            # Use the requests library to send a POST request with proper JSON
            headers = {}
            headers["Authorization"] = f"Bearer NOT_NEEDED"
            print(f"DEBUG: Request URL: {self.base_url}/reset_robot")
            print(f"DEBUG: Request Headers: {headers}")
            print(f"DEBUG: Request Data: {data}")

            response = requests.post(
                f"{self.base_url}/reset_robot", json=data, headers=headers
            )
            # You might also want to print the response status and content
            print(f"DEBUG: Response Status Code: {response.status_code}")
            print(f"DEBUG: Response Text: {response.text}")

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

    def _schedule_messages(self):
        """Schedule messages to be sent at specified times or after checks pass."""
        if not self.messages:
            return

        print(f"Scheduling {len(self.messages)} messages...")

        for message in self.messages:
            if message.get("trigger_type") == "time":
                delay = message.get("time", 0)
                text = message.get("text", "")

                if delay >= 0 and text:
                    # Create a timer to send the message after the specified delay
                    timer = threading.Timer(
                        delay, lambda msg=text: self.websocket_manager.send_message(msg)
                    )
                    timer.daemon = True
                    self.message_timers.append(timer)
                    print(f"Scheduled time-based message at {delay}s: '{text}'")

            # Check-based messages will be handled when checks pass
            elif message.get("trigger_type") == "check":
                check_id = message.get("check_id")
                delay = message.get("delay", 0)
                text = message.get("text", "")
                if check_id and text:
                    print(
                        f"Registered check-based message for check '{check_id}' "
                        f"with delay {delay}s: '{text}'"
                    )
                    # These messages are triggered when the check passes,
                    # handled in check validation

        # Start all timers for time-based messages
        for timer in self.message_timers:
            timer.start()

    def _update_check_status(self, check_id, passed):
        """Update the status of a check and trigger any associated messages."""
        # Record the previous status to detect transitions
        previous_status = self.check_status.get(check_id, False)

        # Update the status
        self.check_status[check_id] = passed

        # If the check just passed (transition from failed to passed)
        if passed and not previous_status:
            # Record when this check was completed
            current_time = time.time()
            self.check_completion_times[check_id] = current_time
            print(f"Check '{check_id}' passed at time {current_time}")

            # Update metrics with check details
            if "checks" not in self.metrics:
                self.metrics["checks"] = {}

            # Find the check configuration from expectations
            check_config = None
            if self.expectations and "checks" in self.expectations:
                for check in self.expectations["checks"]:
                    if check.get("id") == check_id:
                        check_config = check
                        break

            # Create or update the check entry in metrics
            if check_id not in self.metrics["checks"]:
                self.metrics["checks"][check_id] = {
                    "passed": passed,
                    "completion_time": current_time - self.metrics["start_timestamp"],
                }

                # Add check details if available
                if check_config:
                    self.metrics["checks"][check_id]["type"] = check_config.get("type")
                    self.metrics["checks"][check_id]["description"] = check_config.get(
                        "description", ""
                    )
                    # Store other configuration details
                    self.metrics["checks"][check_id]["configuration"] = {
                        k: v
                        for k, v in check_config.items()
                        if k not in ["id", "type", "description"]
                    }
            else:
                # Just update passed status and completion time
                self.metrics["checks"][check_id]["passed"] = passed
                self.metrics["checks"][check_id]["completion_time"] = (
                    current_time - self.metrics["start_timestamp"]
                )

            # For backwards compatibility
            if "check_completion_times" not in self.metrics:
                self.metrics["check_completion_times"] = {}
            self.metrics["check_completion_times"][check_id] = (
                current_time - self.metrics["start_timestamp"]
            )

            # Save updated metrics
            self._save_metrics()

            # Trigger any messages associated with this check
            for message in self.messages:
                if (
                    message.get("trigger_type") == "check"
                    and message.get("check_id") == check_id
                ):
                    delay = message.get("delay", 0)
                    text = message.get("text", "")

                    if text:
                        if delay > 0:
                            # Schedule the message to be sent after the delay
                            timer = threading.Timer(
                                delay,
                                lambda msg=text: self.websocket_manager.send_message(
                                    msg
                                ),
                            )
                            timer.daemon = True
                            self.message_timers.append(timer)
                            timer.start()
                            print(
                                f"Scheduled check-triggered message with {delay}s "
                                f"delay for check '{check_id}': '{text}'"
                            )
                        else:
                            # Send immediately
                            self.websocket_manager.send_message(text)
                            print(
                                f"Sent immediate check-triggered message for "
                                f"check '{check_id}': '{text}'"
                            )

    def _track_robot_position(self):
        """Continuously track the robot's position and store in history."""
        position_interval = 1.0  # Record position every second
        last_position_time = time.time()
        last_save_time = time.time()

        while self.running:
            current_time = time.time()
            if current_time - last_position_time >= position_interval:
                try:
                    # Get current robot position
                    response = requests.get(f"{self.base_url}/get_robot_position")
                    position_data = response.json()

                    # Extract position coordinates
                    if (
                        "position" in position_data
                        and len(position_data["position"]) >= 2
                    ):
                        x = position_data["position"][0]
                        y = position_data["position"][1]

                        # Store position
                        position_entry = (x, y)
                        self.position_history.append(position_entry)

                        # Store timestamp separately (seconds since start)
                        timestamp = current_time - self.metrics["start_timestamp"]
                        self.position_timestamps.append(timestamp)

                        # Save position history periodically to avoid IO overhead
                        if (
                            current_time - last_save_time >= 10.0
                        ):  # Save every 10 seconds
                            self._save_position_history()
                            last_save_time = current_time

                except Exception as e:
                    print(f"Error tracking robot position: {e}")

                last_position_time = current_time

            time.sleep(0.1)  # Sleep to avoid excessive CPU usage

    def _validate_checks(self):
        """Validate all checks in the configuration - called only at end of run."""
        updated_status = validate_checks(
            self.expectations,
            self.check_status,
            self.first_person_dir,
            self.chase_dir,
            self.chat_log,
            self.messages,
            self.metrics,
            self._save_metrics,
            base_url=self.base_url,
            position_history=self.position_history,
        )

        # Handle any newly passed checks
        for check_id, passed in updated_status.items():
            if passed and not self.check_status.get(check_id, False):
                self._update_check_status(check_id, True)

        return updated_status

    def _should_stop_early(self):
        """
        Check if the benchmark should stop early based on the stop criterion.
        Uses a VLM to evaluate the stop criterion against the current state.
        """
        return evaluate_stop_criterion(
            self.early_stop_criterion,
            self.first_person_dir,
            self.chase_dir,
            self.chat_log,
            self.metrics,
            self._save_metrics,
            self.use_frames,
        )

    def _evaluate_final_success(self):
        """
        Evaluate whether the benchmark was successful based on the success criterion.
        Uses a VLM to evaluate the success criterion against the collected data.
        """
        if not self.expectations.get("success_criterion"):
            return {"success": False, "reason": "No success criterion defined"}

        return evaluate_final_success(
            self.expectations["success_criterion"],
            self.first_person_dir,
            self.chase_dir,
            self.chat_log,
            self.metrics,
            self.use_frames,
        )

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
        if not self.websocket_manager.check_brain_status():
            print(
                "Brain check failed. You may want to reset the brain before continuing."
            )
            user_input = input("Continue anyway? (y/n): ")
            if user_input.lower() != "y":
                return False

        # Save metadata
        self._save_metadata()

        # Apply environment parameters (future implementation)
        # For now, just log what would be configured
        if self.current_parameters:
            print(f"Environment: {self.env_name}")
            if "robot_position" in self.current_parameters:
                print(f"Robot position: {self.current_parameters['robot_position']}")
            if "robot_orientation" in self.current_parameters:
                print(
                    f"Robot orientation: {self.current_parameters['robot_orientation']}"
                )
            if "object_positions" in self.current_parameters:
                print(
                    f"Object positions: {self.current_parameters['object_positions']}"
                )

            # TODO: Implement setting of environment parameters
            # This would involve:
            # 1. API calls to set robot position and orientation
            # 2. API calls to place objects at specified positions
            # 3. Verification that environment is set up correctly

        # Reset the robot
        print("Resetting robot...")
        if not self._reset_robot():
            print("Failed to reset robot.")
            return False

        # Wait for reset to complete
        time.sleep(2)

        # If a variant is specified, send a message to switch to it
        if self.variant:
            variant_message = f"!gemini {self.variant}"
            print(f"Sending variant switch message: {variant_message}")

            # Send the variant switch message
            self.websocket_manager.send_message(variant_message)

            # Wait for the switch to take effect
            print("Waiting 3 seconds for variant switch to take effect...")
            time.sleep(3)

        # Send directive
        print(f"Sending directive: '{self.directive}'")
        if not self._send_directive():
            print("Failed to send directive.")
            return False

        # Start data collection
        self.running = True
        self.metrics["start_time"] = datetime.now().isoformat()
        self.metrics["start_timestamp"] = time.time()

        # Start threads for frame capture and chat monitoring
        first_person_thread = threading.Thread(
            target=self._capture_frames, args=("first_person",)
        )
        chase_thread = threading.Thread(target=self._capture_frames, args=("chase",))
        position_thread = threading.Thread(target=self._track_robot_position)

        # Start chat monitoring using WebSocketManager
        self.websocket_manager.running = True
        chat_thread = self.websocket_manager.start_monitoring(
            self.metrics["start_timestamp"]
        )

        self.threads = [first_person_thread, chase_thread, chat_thread, position_thread]
        for thread in [
            first_person_thread,
            chase_thread,
            position_thread,
        ]:  # Chat thread already started
            thread.daemon = True
            thread.start()

        # Schedule messages if any are defined in the configuration
        self._schedule_messages()

        print(f"Benchmark running for {self.duration} seconds...")

        # Run for specified duration or until stop criterion met
        stop_time = time.time() + self.duration

        try:
            while time.time() < stop_time and self.running:
                # Check if we should stop early
                if self._should_stop_early():
                    print("Stop criterion met. Ending benchmark early.")
                    break
                time.sleep(1.0)  # Check conditions every second
        except KeyboardInterrupt:
            print("Benchmark interrupted by user.")
        finally:
            # Stop all threads
            self.running = False
            self.websocket_manager.stop()  # This stops chat monitoring

            for thread in self.threads:
                thread.join(timeout=2.0)

            # Cancel any pending message timers
            for timer in self.message_timers:
                if timer.is_alive():
                    timer.cancel()

            # Record end time
            self.metrics["end_time"] = datetime.now().isoformat()

            # Save final position history to its own file
            self._save_position_history()

            # Validate all checks at the end of the benchmark
            print("Performing validation of all checks at the end of benchmark run...")
            self._validate_checks()

            # Evaluate final success and add to metrics
            success_result = self._evaluate_final_success()
            self.metrics["success"] = success_result

            # Save results - messages are saved in real-time
            self._save_metrics()

            print("Benchmark completed.")
            print(
                f"Captured {self.metrics['frames_captured']['first_person']} "
                f"first-person frames"
            )
            print(f"Captured {self.metrics['frames_captured']['chase']} chase frames")
            print(f"Recorded {self.metrics['chat_messages']} chat messages")
            print(f"Recorded {len(self.position_history)} robot positions")
            print(f"Success: {success_result.get('success', False)}")
            print(f"Reason: {success_result.get('reason', 'No reason provided')}")

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
    parser.add_argument(
        "--variant",
        help="Gemini variant to use",
    )

    args = parser.parse_args()

    benchmark = DirectiveBenchmark(
        config_file=args.config,
        trial_num=args.trial,
        base_url=args.url,
        frame_capture_interval=args.interval,
        variant=args.variant,
    )

    benchmark.run()


if __name__ == "__main__":
    main()
