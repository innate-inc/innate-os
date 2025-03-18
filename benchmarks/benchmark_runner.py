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
from openai import OpenAI
from dotenv import load_dotenv


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
        self.stop_criterion = self.expectations.get("stop_criterion", None)

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

        # WebSocket connection for sending messages
        self.chat_ws = None
        self.chat_ws_lock = threading.Lock()  # Lock for thread safety

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
        user_id = "monitor"
        email = "benchmark@example.com"
        user_params = f"user_id={user_id}&email={email}"
        ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"
        print(f"Connecting to monitoring WebSocket: {ws_url}")

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
                        pass
            except Exception as e:
                print(f"Error processing message: {e}")

        # Error handler for WebSocket
        def on_error(ws, error):
            print(f"Monitoring WebSocket error: {error}")

        # Connection close handler
        def on_close(ws, close_status_code, close_msg):
            print("Monitoring WebSocket connection closed")

        # Connection open handler
        def on_open(ws):
            print("Monitoring WebSocket connection established")

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
            print(f"Error setting up monitoring WebSocket: {e}")
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
        user_id = "brain_check"
        email = "benchmark@example.com"
        user_params = f"user_id={user_id}&email={email}"
        ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"
        print(f"Connecting to brain check WebSocket: {ws_url}")

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
                on_open=lambda ws: print(
                    "Brain check WebSocket connection established"
                ),
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

    def _initialize_chat_connection(self):
        """Initialize a persistent WebSocket connection for sending messages."""
        try:
            # Create a WebSocket URL
            base_url_no_http = self.base_url.replace("http://", "")
            user_id = "benchmark"
            email = "benchmark@example.com"
            user_params = f"user_id={user_id}&email={email}"
            ws_url = f"ws://{base_url_no_http}/ws/chat?{user_params}"

            print(f"Initializing persistent WebSocket connection: {ws_url}")

            # Create WebSocket connection
            self.chat_ws = websocket.create_connection(ws_url)
            print("Persistent WebSocket connection established")
            return True
        except Exception as e:
            print(f"Error initializing WebSocket connection: {e}")
            self.chat_ws = None
            return False

    def _close_chat_connection(self):
        """Close the persistent WebSocket connection."""
        with self.chat_ws_lock:
            if self.chat_ws:
                try:
                    self.chat_ws.close()
                    print("Persistent WebSocket connection closed")
                except Exception as e:
                    print(f"Error closing WebSocket connection: {e}")
                finally:
                    self.chat_ws = None

    def _send_message(self, message_text):
        """Send a message to the robot using the persistent WebSocket connection."""
        with self.chat_ws_lock:
            try:
                # If connection doesn't exist or is closed, initialize it
                if not self.chat_ws:
                    if not self._initialize_chat_connection():
                        return False

                # Send message - just send the text directly, not a JSON object
                self.chat_ws.send(message_text)
                print(f"Message sent via WebSocket: '{message_text}'")
                return True
            except Exception as e:
                print(f"Error sending message via WebSocket: {e}")
                # Try to re-establish connection on next send
                self._close_chat_connection()
                return False

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
                        delay, lambda msg=text: self._send_message(msg)
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
                                delay, lambda msg=text: self._send_message(msg)
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
                            self._send_message(text)
                            print(
                                f"Sent immediate check-triggered message for "
                                f"check '{check_id}': '{text}'"
                            )

    # TODO: Implement check validation methods for different check types
    def _validate_location_check(self, check_id, check_data):
        """
        Validate if the robot is within the specified location bounding box.
        This is a placeholder for future implementation.
        """
        # TODO: Implement location validation using robot position data
        return False

    # TODO: Implement primitive call validation
    def _validate_primitive_check(self, check_id, check_data):
        """
        Validate if the specified primitive was called with appropriate arguments.
        This is a placeholder for future implementation.
        """
        # TODO: Implement primitive call validation using LLM for argument verification
        return False

    # TODO: Implement compound check validation
    def _validate_compound_check(self, check_id, check_data):
        """
        Validate if a primitive was called while in a specific location.
        This is a placeholder for future implementation.
        """
        # TODO: Implement compound validation logic
        return False

    # TODO: Implement sequence check validation
    def _validate_sequence_check(self, check_id, check_data):
        """
        Validate if a sequence of checks occurred in the specified order.
        This is a placeholder for future implementation.
        """
        # TODO: Implement sequence validation logic
        return False

    def _validate_vlm_check(self, check_id, check_data):
        """
        Use a VLM to verify a specific aspect of the robot's behavior.

        Args:
            check_id (str): Identifier for the check
            check_data (dict): Check configuration data

        Returns:
            bool: True if the check passes, False otherwise
        """
        # Get verification prompt from check data
        verification_prompt = check_data.get("verification_prompt")
        if not verification_prompt:
            print(f"Error: No verification prompt for VLM check '{check_id}'")
            return False

        # Get representative frames for analysis
        frames = self._get_representative_frames()
        if not frames:
            print(f"Warning: No frames available for VLM check '{check_id}'")
            return False

        # Evaluate using VLM
        try:
            result = self._evaluate_with_vlm(
                verification_prompt, frames, is_stop_check=False
            )

            # Log the result
            passed = result.get("success", False)
            reason = result.get("reason", "No reason provided")

            if passed:
                print(f"Check '{check_id}' passed: {reason}")
            else:
                print(f"Check '{check_id}' failed: {reason}")

            # Store the result in metrics
            if "check_results" not in self.metrics:
                self.metrics["check_results"] = {}

            self.metrics["check_results"][check_id] = {
                "passed": passed,
                "time": time.time() - self.metrics["start_timestamp"],
                "reason": reason,
            }
            self._save_metrics()

            return passed

        except Exception as e:
            print(f"Error in VLM check '{check_id}': {e}")
            return False

    def _should_stop_early(self):
        """
        Check if the benchmark should stop early based on the stop criterion.
        Uses a VLM to evaluate the stop criterion against the current state.
        """
        if not self.stop_criterion:
            return False

        # Get representative frames from the benchmark so far
        frames = self._get_representative_frames()
        if not frames:
            return False

        # Evaluate the stop criterion using VLM
        result = self._evaluate_with_vlm(
            self.stop_criterion, frames, is_stop_check=True
        )

        if result.get("should_stop", False):
            # Log the reason for stopping
            print(f"Stop criterion met: {result.get('reason', 'Unknown reason')}")

            # Save the stop decision to metrics
            self.metrics["early_stop"] = {
                "triggered": True,
                "time": time.time() - self.metrics["start_timestamp"],
                "reason": result.get("reason", "Unknown reason"),
            }
            self._save_metrics()

            return True

        return False

    def _evaluate_final_success(self):
        """
        Evaluate whether the benchmark was successful based on the success criterion.
        Uses a VLM to evaluate the success criterion against the collected data.
        """
        if not self.expectations.get("success_criterion"):
            return {"success": False, "reason": "No success criterion defined"}

        # Get representative frames from the benchmark
        frames = self._get_representative_frames(comprehensive=True)
        if not frames:
            return {"success": False, "reason": "No frames available for evaluation"}

        # Evaluate the success criterion using VLM
        return self._evaluate_with_vlm(
            self.expectations["success_criterion"], frames, is_stop_check=False
        )

    def _get_representative_frames(self, comprehensive=False):
        """
        Select representative frames from the benchmark for VLM evaluation.

        Args:
            comprehensive (bool): If True, includes more frames for a more
            thorough evaluation

        Returns:
            list: Paths to selected image frames
        """
        # TODO: Implement intelligent frame selection for VLM analysis
        # For now, just select a few frames at regular intervals

        first_person_frames = sorted(list(self.first_person_dir.glob("*.jpg")))
        chase_frames = sorted(list(self.chase_dir.glob("*.jpg")))

        if not first_person_frames and not chase_frames:
            return []

        # Select frames at regular intervals
        interval = 5 if comprehensive else 20  # More frames for comprehensive analysis

        # Combine frames from both cameras, alternating
        selected_frames = []
        max_frames = min(len(first_person_frames), len(chase_frames))

        for i in range(0, max_frames, interval):
            if i < len(first_person_frames):
                selected_frames.append(str(first_person_frames[i]))
            if i < len(chase_frames):
                selected_frames.append(str(chase_frames[i]))

        return selected_frames[:20]  # Limit to 20 frames to avoid token limits

    def _encode_image(self, image_path):
        """
        Encode an image to base64 for VLM API.

        Args:
            image_path (str): Path to the image file

        Returns:
            str: Base64 encoded image
        """
        try:
            import base64

            with open(image_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                return encoded_string
        except Exception as e:
            print(f"Error encoding image {image_path}: {e}")
            return None

    def _evaluate_with_vlm(self, criterion, frame_paths, is_stop_check=False):
        """
        Evaluate a criterion using a VLM model with the given frames.

        Args:
            criterion (str): The criterion to evaluate
            frame_paths (list): Paths to image frames
            is_stop_check (bool): Whether this is a stop criterion check

        Returns:
            dict: Structured result with success/should_stop and reason fields
        """
        # Load the API key from .env file
        load_dotenv("benchmarks/.env")
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise ValueError("No VLM API key provided in benchmarks/.env")

        try:
            # Create OpenAI client
            client = OpenAI(api_key=api_key)

            # Create schema for structured output
            result_key = "should_stop" if is_stop_check else "success"
            schema = {
                "type": "object",
                "properties": {
                    result_key: {
                        "type": "boolean",
                        "description": "Whether the criterion has been met",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Detailed explanation of why the criterion was met or not met",
                    },
                },
                "required": [result_key, "reason"],
                "additionalProperties": False,
            }

            # Encode images to base64
            content = []

            # Add the system message
            prompt_type = "stop" if is_stop_check else "success"
            system_prompt = (
                f"You are an AI evaluator for robot benchmarks. You will receive frames "
                f"showing a robot performing tasks. Evaluate whether the {prompt_type} "
                f"criterion has been met based on the images provided."
            )
            content.append({"role": "system", "content": system_prompt})

            # Build the user message with images
            user_content = [
                {
                    "type": "text",
                    "text": f"Based on the provided frames, evaluate the following "
                    f"{prompt_type} criterion:\n\n{criterion}",
                }
            ]

            # Add images to the content
            for frame_path in frame_paths:
                encoded_image = self._encode_image(frame_path)
                if encoded_image:
                    user_content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{encoded_image}"
                            },
                        }
                    )

            # Add the user message with all images
            content.append({"role": "user", "content": user_content})

            # Call the OpenAI API
            response = client.responses.create(
                model="gpt-4o-2024-08-06",
                input=content,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "criterion_evaluation",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )

            # Parse the structured output
            result = (
                response.output_text.dict()
                if hasattr(response.output_text, "dict")
                else response.output_text
            )

            # If response is a string (JSON), parse it
            if isinstance(result, str):
                import json

                result = json.loads(result)

            print(f"VLM evaluation result: {result}")
            return result

        except Exception as e:
            print(f"Error in VLM evaluation: {e}")
            if is_stop_check:
                return {"should_stop": False, "reason": f"Error in VLM evaluation: {e}"}
            else:
                return {"success": False, "reason": f"Error in VLM evaluation: {e}"}

    def _validate_checks(self):
        """Validate all checks in the configuration."""
        if not self.expectations.get("checks"):
            return

        print("Validating checks...")
        for check in self.expectations["checks"]:
            check_id = check.get("id")
            check_type = check.get("type")

            if not check_id or not check_type:
                continue

            # Skip checks that have already passed
            if self.check_status.get(check_id, False):
                continue

            # Validate based on check type
            passed = False
            if check_type == "location":
                passed = self._validate_location_check(check_id, check)
            elif check_type == "primitive":
                passed = self._validate_primitive_check(check_id, check)
            elif check_type == "compound":
                passed = self._validate_compound_check(check_id, check)
            elif check_type == "sequence":
                passed = self._validate_sequence_check(check_id, check)
            elif check_type == "vlm_verification":
                passed = self._validate_vlm_check(check_id, check)
            else:
                print(
                    f"Warning: Unknown check type '{check_type}' for check '{check_id}'"
                )
                continue

            # Update check status and trigger any associated messages
            if passed:
                self._update_check_status(check_id, True)

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

        # Send directive
        print(f"Sending directive: '{self.directive}'")
        if not self._send_directive():
            print("Failed to send directive.")
            return False

        # Initialize persistent WebSocket connection for chat messages
        self._initialize_chat_connection()

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

        # Run for specified duration or until stop criterion met
        stop_time = time.time() + self.duration
        last_check_time = time.time()
        check_interval = 15  # seconds between check validations

        try:
            while time.time() < stop_time and self.running:
                # Validate checks periodically
                current_time = time.time()
                if current_time - last_check_time >= check_interval:
                    self._validate_checks()
                    last_check_time = current_time

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
            for thread in self.threads:
                thread.join(timeout=2.0)

            # Cancel any pending message timers
            for timer in self.message_timers:
                if timer.is_alive():
                    timer.cancel()

            # Close the persistent WebSocket connection
            self._close_chat_connection()

            # Record end time
            self.metrics["end_time"] = datetime.now().isoformat()

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
