# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, Dict

import numpy as np
import torch
import zmq
import traceback

class TorchSerializer:
    @staticmethod
    def to_bytes(data: dict) -> bytes:
        buffer = BytesIO()
        torch.save(data, buffer)
        return buffer.getvalue()

    @staticmethod
    def from_bytes(data: bytes) -> dict:
        buffer = BytesIO(data)
        # Ensure map_location is set to handle potential device mismatches
        # and weights_only=False to load arbitrary python objects
        obj = torch.load(buffer, map_location="cpu", weights_only=False)
        return obj


@dataclass
class EndpointHandler:
    handler: Callable
    requires_input: bool = True


class BaseInferenceClient:
    def __init__(
        self, host: str = "localhost", port: int = 5555, timeout_ms: int = 15000
    ):
        self.context = zmq.Context()
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self._init_socket()

    def _init_socket(self):
        """Initialize or reinitialize the socket with current settings"""
        # Close existing socket if it exists
        if hasattr(self, "socket") and not self.socket.closed:
            self.socket.close()
        self.socket = self.context.socket(zmq.REQ)
        # Set linger to 0 to prevent hanging on close
        self.socket.setsockopt(zmq.LINGER, 0)
        # Set timeout
        self.socket.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self.socket.connect(f"tcp://{self.host}:{self.port}")
        print(f"Client connected to tcp://{self.host}:{self.port}")

    def ping(self) -> bool:
        try:
            self.call_endpoint("ping", requires_input=False)
            print("Ping successful.")
            return True
        except zmq.error.Again:
            print(
                f"Ping timed out after {self.timeout_ms} ms. Server might be busy or down."
            )
            self._init_socket()  # Recreate socket for next attempt
            return False
        except zmq.error.ZMQError as e:
            print(f"Ping failed with ZMQ error: {e}. Re-initializing socket.")
            self._init_socket()  # Recreate socket for next attempt
            return False
        except Exception as e:
            print(f"Ping failed with unexpected error: {e}. Re-initializing socket.")
            self._init_socket()  # Recreate socket for next attempt
            return False

    def kill_server(self):
        """
        Kill the server.
        """
        try:
            self.call_endpoint("kill", requires_input=False)
            print("Kill signal sent to server.")
        except Exception as e:
            print(f"Failed to send kill signal: {e}")

    def call_endpoint(
        self, endpoint: str, data: dict | None = None, requires_input: bool = True
    ) -> dict:
        """
        Call an endpoint on the server.

        Args:
            endpoint: The name of the endpoint.
            data: The input data for the endpoint.
            requires_input: Whether the endpoint requires input data.
        """
        request: dict = {"endpoint": endpoint}
        if requires_input:
            if data is None:
                raise ValueError("Data must be provided when requires_input is True")
            request["data"] = data

        try:
            print(f"Sending request to endpoint '{endpoint}'...")
            time_start = time.time()
            self.socket.send(TorchSerializer.to_bytes(request))
            time_end = time.time()
            print(f"Serialization complete in {(time_end - time_start) * 1000:.4f} ms.")

            print("Waiting for reply...")
            message = self.socket.recv()

            if message == b"ERROR":
                print("Received error signal from server.")
                raise RuntimeError("Server error")

            print("Reply received, deserializing...")
            time_start = time.time()
            result = TorchSerializer.from_bytes(message)
            time_end = time.time()
            print(f"Deserialization complete in {(time_end - time_start) * 1000:.4f} ms.")
            return result

        except zmq.error.Again:  # Handle timeout
            print(
                f"Request timed out after {self.timeout_ms} ms. Re-initializing socket."
            )
            self._init_socket()  # Recreate socket for next attempt
            raise TimeoutError(f"Server did not respond within {self.timeout_ms}ms")
        except zmq.error.ZMQError as e:
            print(f"ZMQ Error during call_endpoint: {e}. Re-initializing socket.")
            self._init_socket()  # Recreate socket for next attempt
            raise e  # Re-raise the exception
        except Exception as e:
            print(
                f"Unexpected error during call_endpoint: {e}. Re-initializing socket."
            )
            print(traceback.format_exc())
            self._init_socket()  # Recreate socket for next attempt
            raise e  # Re-raise the exception

    def __del__(self):
        """Cleanup resources on destruction"""
        if hasattr(self, "socket") and not self.socket.closed:
            print("Closing client socket.")
            self.socket.close()
        if hasattr(self, "context") and not self.context.closed:
            print("Terminating ZMQ context.")
            self.context.term()


class StandaloneRobotClient(BaseInferenceClient):
    """
    Client for communicating with the RobotInferenceServer
    """

    def get_action(self, observations: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get the action from the server.
        The exact definition of the observations is defined
        by the policy running on the server.
        """
        return self.call_endpoint("get_action", observations)

    def get_modality_config(self) -> Dict[str, Any]:
        """
        Get the modality configuration from the server.
        Returns a dictionary where keys are modality names and values
        are their configurations (structure depends on the server's policy).
        """
        return self.call_endpoint("get_modality_config", requires_input=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone GR00T Inference Client")
    parser.add_argument(
        "--port", type=int, help="Port number for the server.", default=5555
    )
    parser.add_argument(
        "--host", type=str, help="Host address for the server.", default="localhost"
    )
    args = parser.parse_args()

    # Create a client instance
    policy_client = StandaloneRobotClient(host=args.host, port=args.port)

    # --- Test Ping ---
    print("--- Testing Ping ---")
    if policy_client.ping():
        print("Server is alive.")
    else:
        print("Server did not respond to ping.")
        # Exit if ping fails, as subsequent calls will likely fail too
        exit(1)

    # --- Get Modality Config ---
    print("--- Getting Modality Config ---")
    try:
        modality_configs = policy_client.get_modality_config()
        print("Available modality config keys:")
        print(list(modality_configs.keys()))
    except Exception as e:
        print(f"Failed to get modality config: {e}")
        # Optionally exit or handle error

    # --- Get Action ---
    print("--- Getting Action ---")
    # Example observation data based on my_robot_dataset/meta/modality.json
    obs = {
        # State modalities
        "state.qpos": np.random.rand(1, 6).astype(
            np.float32
        ),  # Shape (1, 6) from modality.json
        "state.qvel": np.random.rand(1, 6).astype(
            np.float32
        ),  # Shape (1, 6) from modality.json
        # Video modalities
        "video.ego_view": np.random.randint(
            0, 256, (1, 256, 256, 3), dtype=np.uint8
        ),  # Placeholder shape
        "video.gripper_view": np.random.randint(
            0, 256, (1, 128, 128, 3), dtype=np.uint8
        ),  # Placeholder shape
        # Annotation modalities
        "annotation.human.action.task_description": [
            "pick up the cube"
        ],  # Example task
        "annotation.human.validity": np.array(
            [1.0], dtype=np.float32
        ),  # Placeholder validity
    }
    print("Sending observation data...")
    for key, value in obs.items():
        if isinstance(value, np.ndarray):
            print(f"  {key}: shape={value.shape}, dtype={value.dtype}")
        else:
            print(f"  {key}: {value}")

    try:
        time_start = time.time()
        action = policy_client.get_action(obs)
        time_end = time.time()
        print(f"Action received successfully in {time_end - time_start:.4f} seconds.")

        print("Received action shapes:")
        for key, value in action.items():
            if isinstance(value, np.ndarray):
                print(f"  Action '{key}': shape={value.shape}, dtype={value.dtype}")
            else:
                print(f"  Action '{key}': {value}")

    except TimeoutError:
        print("Getting action timed out. Check server status and network.")
    except RuntimeError as e:
        print(f"Server returned an error: {e}")
    except Exception as e:
        print(f"An unexpected error occurred while getting action: {e}")
        import traceback

        print(traceback.format_exc())

    # --- Test Kill Server (Optional) ---
    # print("--- Sending Kill Signal ---")
    # policy_client.kill_server()
    # print("Kill signal sent. Note: Server might take a moment to shut down.")

    print("Client finished.")
