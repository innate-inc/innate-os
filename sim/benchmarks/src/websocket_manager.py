#!/usr/bin/env python3
import json
import time
import threading
import websocket
from typing import Dict, List, Callable, Any


class WebSocketManager:
    """
    Manages WebSocket connections for chat communication with the robot simulation.
    """

    def __init__(self, base_url, save_chat_message_callback):
        self.base_url = base_url
        self.base_url_no_http = base_url.replace("http://", "")
        self.save_chat_message_callback = save_chat_message_callback

        # WebSocket connection for sending messages
        self.chat_ws = None
        self.chat_ws_lock = threading.Lock()  # Lock for thread safety

        # Control flag
        self.running = False

    def initialize_chat_connection(self):
        """Initialize a persistent WebSocket connection for sending messages."""
        try:
            # Create a WebSocket URL
            user_id = "benchmark"
            email = "benchmark@example.com"
            user_params = f"user_id={user_id}&email={email}"
            ws_url = f"ws://{self.base_url_no_http}/ws/chat?{user_params}"

            print(f"Initializing persistent WebSocket connection: {ws_url}")

            # Create WebSocket connection
            self.chat_ws = websocket.create_connection(ws_url)
            print("Persistent WebSocket connection established")
            return True
        except Exception as e:
            print(f"Error initializing WebSocket connection: {e}")
            self.chat_ws = None
            return False

    def close_chat_connection(self):
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

    def send_message(self, message_text):
        """Send a message to the robot using the persistent WebSocket connection."""
        with self.chat_ws_lock:
            try:
                # If connection doesn't exist or is closed, initialize it
                if not self.chat_ws:
                    if not self.initialize_chat_connection():
                        return False

                # Send message - just send the text directly, not a JSON object
                self.chat_ws.send(message_text)
                print(f"Message sent via WebSocket: '{message_text}'")
                return True
            except Exception as e:
                print(f"Error sending message via WebSocket: {e}")
                # Try to re-establish connection on next send
                self.close_chat_connection()
                return False

    def monitor_chat(self, start_timestamp):
        """Monitor and record chat messages using WebSockets."""
        # Create a WebSocket URL
        user_id = "monitor"
        email = "benchmark@example.com"
        user_params = f"user_id={user_id}&email={email}"
        ws_url = f"ws://{self.base_url_no_http}/ws/chat?{user_params}"
        print(f"Connecting to monitoring WebSocket: {ws_url}")

        # Message handler for WebSocket
        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "sender" in data and "text" in data:
                    # Check if the message has a timestamp
                    msg_time = data.get("timestamp", time.time())

                    # Only process messages that occurred after the test started
                    if msg_time >= start_timestamp:
                        self.save_chat_message_callback(data)
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
            self.save_chat_message_callback(placeholder_msg)

            # Keep the thread running for the duration of the benchmark
            while self.running:
                time.sleep(1.0)

    def check_brain_status(self):
        """
        Check if the brain is running properly by monitoring initial chat messages.
        Returns True if the brain appears to be functioning, False otherwise.
        """
        print("Checking brain status...")

        # Create a WebSocket URL for monitoring brain status
        user_id = "brain_check"
        email = "benchmark@example.com"
        user_params = f"user_id={user_id}&email={email}"
        ws_url = f"ws://{self.base_url_no_http}/ws/chat?{user_params}"
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

    def start_monitoring(self, start_timestamp):
        """Start monitoring chat in a separate thread."""
        self.running = True
        monitor_thread = threading.Thread(
            target=self.monitor_chat, args=(start_timestamp,)
        )
        monitor_thread.daemon = True
        monitor_thread.start()
        return monitor_thread

    def stop(self):
        """Stop all monitoring and close connections."""
        self.running = False
        self.close_chat_connection()
