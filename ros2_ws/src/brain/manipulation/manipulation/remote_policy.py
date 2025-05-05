#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray  # For arm commands
from cv_bridge import CvBridge
import numpy as np
import cv2
from geometry_msgs.msg import Twist
import argparse

# Import the remote inference client
from manipulation.remote_inference_service import StandaloneRobotClient

# Import the service type for initial pose
from maurice_msgs.srv import GotoJS

# Define configurations
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5555
INFERENCE_HZ = 30  # Frequency to request actions


class RemoteInferenceNode(Node):
    def __init__(self, host: str, port: int):
        super().__init__("remote_inference_node")
        self.get_logger().info(
            f"Remote Inference node started. Connecting to {host}:{port}"
        )
        self.bridge = CvBridge()
        self.image_size = (256, 256)  # Assuming the remote policy expects this size
        self.gripper_image_size = (128, 128)  # Assuming a gripper view is also needed

        # Initialize the policy client
        self.policy_client = StandaloneRobotClient(host=host, port=port)

        # Check server connection
        if not self.policy_client.ping():
            self.get_logger().error(
                "Failed to connect to the remote inference server. Shutting down."
            )
            # TODO: Implement more robust handling, e.g., retries or specific exception
            rclpy.shutdown()
            return  # Exit __init__ if connection fails

        # Get modality config (optional, but good practice)
        try:
            self.modality_config = self.policy_client.get_modality_config()
            self.get_logger().info(
                f"Received modality config: {list(self.modality_config.keys())}"
            )
            # You might want to use self.modality_config to adapt image sizes, etc.
        except Exception as e:
            self.get_logger().warn(f"Could not get modality config: {e}")
            self.modality_config = None  # Set to None if failed

        # Variables to hold the latest sensor data
        self.latest_image1 = None  # e.g., 'video.ego_view'
        self.latest_image2 = None  # e.g., 'video.gripper_view'
        self.latest_joint_state = None  # 'state.qpos' and 'state.qvel'

        # Subscribers for images and joint state
        # Adapt topics based on actual setup and modality config
        self.create_subscription(
            Image, "/color/image", self.image1_callback, 10
        )  # ego_view?
        self.create_subscription(
            Image, "/image_raw", self.image2_callback, 10
        )  # gripper_view?
        self.create_subscription(
            JointState, "/maurice_arm/state", self.joint_state_callback, 10
        )

        # Publishers for twist and arm commands
        self.cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.arm_state_pub = self.create_publisher(
            Float64MultiArray, "/maurice_arm/commands", 10
        )

        # Timer to run the inference loop
        self.timer = self.create_timer(1.0 / INFERENCE_HZ, self.inference_loop)

        # Call the /maurice_arm/goto_js service at startup
        self.call_goto_js_service()

    ####################################################
    # Callback Methods for Sensor Data
    ####################################################
    def image1_callback(self, msg: Image):
        try:
            # Assuming this is the ego view
            cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
            self.latest_image1 = cv2.resize(cv_image, self.image_size)
        except Exception as e:
            self.get_logger().error(f"Error converting image1 (ego_view): {e}")

    def image2_callback(self, msg: Image):
        try:
            # Assuming this is the gripper view
            cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
            self.latest_image2 = cv2.resize(cv_image, self.gripper_image_size)
        except Exception as e:
            self.get_logger().error(f"Error converting image2 (gripper_view): {e}")

    def joint_state_callback(self, msg: JointState):
        # Store the entire message, we need position and velocity
        self.latest_joint_state = msg

    ####################################################
    # Service Call for Initial Joint State
    ####################################################
    def call_goto_js_service(self):
        self.goto_client = self.create_client(GotoJS, "/maurice_arm/goto_js")
        while not self.goto_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info(
                "Waiting for /maurice_arm/goto_js service to become available..."
            )
        req = GotoJS.Request()
        # Send to a default starting position (e.g., all zeros)
        # The remote policy might have its own preferred starting state.
        self.get_logger().warn("Using default zero joint state for initialization.")
        req.data = Float64MultiArray(data=[0.0] * 6)  # Assuming 6 DoF arm
        req.time = 2  # Allow 2 seconds to reach the position
        future = self.goto_client.call_async(req)
        future.add_done_callback(self.goto_response_callback)

    def goto_response_callback(self, future):
        try:
            response = future.result()
            if response.success:
                self.get_logger().info("GotoJS service call succeeded.")
            else:
                self.get_logger().warn("GotoJS service call failed.")
        except Exception as e:
            self.get_logger().error(f"Service call failed: {e}")

    ####################################################
    # Inference Loop (using Remote Client)
    ####################################################
    def inference_loop(self):
        start_time = time.time()

        # Check if all required sensor data is available
        if (
            self.latest_image1 is None
            or self.latest_image2 is None
            or self.latest_joint_state is None
        ):
            self.get_logger().info("Waiting for all sensor topics...")
            # Consider adding a check to ping server if data is missing for too long
            return

        # Prepare observation dictionary based on expected modalities
        # Adapt keys ('video.ego_view', etc.) based on the actual remote policy config
        try:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            qvel = np.array(self.latest_joint_state.velocity, dtype=np.float32)
            # Add batch dimension (policy likely expects batch_size=1)
            obs = {
                "video.ego_view": np.expand_dims(self.latest_image1, axis=0),
                "video.gripper_view": np.expand_dims(self.latest_image2, axis=0),
                "state.qpos": np.expand_dims(qpos, axis=0),
                "state.qvel": np.expand_dims(qvel, axis=0),
                # Add other required modalities if needed, e.g., task description
                # "annotation.human.action.task_description": ["perform task"],
            }
        except Exception as e:
            self.get_logger().error(f"Error preparing observation data: {e}")
            return

        # --- Call Remote Inference ---
        action_dict = None
        try:
            action_dict = self.policy_client.get_action(obs)
        except TimeoutError:
            self.get_logger().error("Remote inference timed out. Skipping cycle.")
            # Consider trying to reconnect or pinging the server here
            return
        except RuntimeError as e:
            self.get_logger().error(f"Remote server returned an error: {e}")
            return
        except Exception as e:
            self.get_logger().error(f"Failed to get action from remote server: {e}")
            # Consider more specific error handling or reconnection logic
            return

        if action_dict is None or "action" not in action_dict:
            self.get_logger().error("Invalid action received from server.")
            return

        # --- Process and Publish Action ---
        try:
            # Assuming the action is returned under the key 'action'
            # and is a numpy array [batch, action_dim]
            next_action = action_dict["action"]
            if isinstance(next_action, np.ndarray):
                # Remove batch dimension if present
                if next_action.ndim > 1:
                    next_action = next_action.squeeze(0)
                next_action = next_action.tolist()  # Convert to list
            else:
                # Handle non-numpy array actions
                self.get_logger().warn(f"Action not numpy array: {type(next_action)}")
                if not isinstance(next_action, list):
                    self.get_logger().error("Action format unusable.")
                    return

            # Ensure the action has the expected dimension (e.g., 8 for 6DoF + base)
            # This depends heavily on the remote policy output spec
            expected_dim = 8  # Example: 6 arm joints + linear_x + angular_z
            if len(next_action) != expected_dim:
                self.get_logger().error(
                    f"Received action has wrong dimension: {len(next_action)}, expected {expected_dim}"
                )
                return

            self.get_logger().debug(
                f"Received action: {next_action}"
            )  # Use debug level

            # Extract and publish the twist command (last two elements)
            twist_msg = Twist()
            # Apply scaling if needed, based on policy training/output range
            twist_msg.linear.x = float(next_action[-2])  # / 2 # Example scaling
            twist_msg.angular.z = float(next_action[-1])  # / 2 # Example scaling
            self.cmd_vel_pub.publish(twist_msg)

            # Extract and publish the arm command (first six elements)
            arm_msg = Float64MultiArray()
            arm_msg.data = [float(a) for a in next_action[:6]]
            self.arm_state_pub.publish(arm_msg)

        except IndexError:
            self.get_logger().error("Error processing action: Index out of bounds.")
        except TypeError:
            self.get_logger().error("Error processing action: Type mismatch.")
        except Exception as e:
            self.get_logger().error(f"Unexpected error processing action: {e}")

        cycle_time = time.time() - start_time
        self.get_logger().info(f"Inference cycle time: {cycle_time:.3f} seconds")
        if cycle_time > (1.0 / INFERENCE_HZ):
            self.get_logger().warn(
                f"Cycle time ({cycle_time:.3f}s) exceeded target rate ({1.0/INFERENCE_HZ:.3f}s)"
            )


def main(args=None):
    rclpy.init(args=args)

    parser = argparse.ArgumentParser(description="Remote Policy Client Node")
    parser.add_argument(
        "--host", type=str, default=DEFAULT_HOST, help="Inference server host"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Inference server port"
    )
    # Use parse_known_args to avoid conflicts with ROS arguments
    parsed_args, _ = parser.parse_known_args()

    node = RemoteInferenceNode(host=parsed_args.host, port=parsed_args.port)

    try:
        # Only spin if node initialization (including ping) was successful
        if rclpy.ok():
            rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Remote inference node shutting down.")
    finally:
        # Ensure node is destroyed only if it was successfully created
        if (
            "node" in locals() and node.executor is not None
        ):  # Check if node was fully initialized
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
