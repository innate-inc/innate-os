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
from remote_inference_service import StandaloneRobotClient

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
        self.image_size = (640, 480)  # Assuming the remote policy expects this size
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
        self.latest_image2 = np.zeros((480, 640, 3), dtype=np.uint8)  # Mock image for gripper view
        # Mock joint state with 6 positions and velocities initialized to zero
        self.latest_joint_state = JointState(
            position=[0.0] * 6,  # 6 joint positions
            velocity=[0.0] * 6   # 6 joint velocities
        ) # TODO: THIS IS MOCKED AND NEEDS TO BE REPLACED WITH THE REAL JOINT STATE
        # CRITICAL ERROR: Mock joint state is being used! This needs to be replaced with real joint state ASAP
        # This is a temporary mock that will cause incorrect behavior - high priority to fix
        self.get_logger().error(
            "\033[1;31m"  # Red text
            "CRITICAL: Using mock joint state data - this must be replaced with real joint state immediately! "
            "Current implementation will cause incorrect robot behavior."
            "\033[0m"  # Reset color
        )

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

        if action_dict is None or not all(k in action_dict for k in ["action.single_arm", "action.gripper", "action.base_motion"]):
            self.get_logger().error("Invalid or incomplete action received from server.")
            return

        # --- Process and Publish Action ---
        try:
            # Extract actions based on the new keys
            single_arm_action = action_dict["action.single_arm"]
            gripper_action = action_dict["action.gripper"] # Gripper action might need separate handling
            base_action = action_dict["action.base_motion"]

            # --- Helper to process potential numpy arrays ---
            def process_action_array(action_array):
                """Process action array (NumPy or list) to get the 1D list for the first time step."""
                processed_list = None
                if isinstance(action_array, np.ndarray):
                    if action_array.ndim >= 1:
                        # Select the first element along the first dimension (time step)
                        first_step_action = action_array[0]
                        if isinstance(first_step_action, np.ndarray):
                             processed_list = first_step_action.tolist() # Convert the selected array to list
                        else: # Handle case where slicing results in a scalar
                             processed_list = [float(first_step_action)] # Wrap scalar in list
                    else:
                        # Handle 0-dimensional array? Log error for now.
                        self.get_logger().error(f"Action part is a 0-dimensional numpy array: {action_array}")
                        return None # Indicate error
                elif isinstance(action_array, list):
                    if action_array and isinstance(action_array[0], list): # Check if it's a list of lists
                        # Select the first inner list (first time step)
                        processed_list = action_array[0]
                    elif action_array: # It's likely a 1D list already
                        processed_list = action_array
                    else: # Empty list
                        processed_list = [] # Return empty list
                else:
                    self.get_logger().error(f"Action part is not a list or numpy array: {type(action_array)}")
                    return None # Indicate error

                # Final check if the result is a list
                if not isinstance(processed_list, list):
                     self.get_logger().error(f"Failed to process action part into a list. Result: {processed_list}")
                     return None

                return processed_list

            single_arm_list = process_action_array(single_arm_action)
            gripper_list = process_action_array(gripper_action) # Process gripper
            base_list = process_action_array(base_action)

            # --- Arm Command ---
            # Assuming single_arm_list contains the 6 DoF commands
            arm_msg = Float64MultiArray()
            # Take the first 6 elements for the arm
            arm_msg.data = [float(a) for a in single_arm_list[:6]] # Directly use 6
            self.arm_state_pub.publish(arm_msg)
            self.get_logger().debug(f"Published arm command: {arm_msg.data}")

            # --- Gripper Command (Example: log it for now) ---
            gripper_command = float(gripper_list[0]) # Directly access first element
            self.get_logger().debug(f"Received gripper command: {gripper_command}")
            # TODO: Publish gripper command if needed, e.g., to a separate topic or service


            # --- Base Command ---
            # Assuming base_list contains [linear_x, angular_z]
            twist_msg = Twist()
            # Apply scaling if needed, based on policy training/output range
            twist_msg.linear.x = float(base_list[0]) # Directly access first element
            twist_msg.angular.z = float(base_list[1]) # Directly access second element
            self.cmd_vel_pub.publish(twist_msg)
            self.get_logger().debug(f"Published twist command: linear.x={twist_msg.linear.x}, angular.z={twist_msg.angular.z}")


        except KeyError as e:
            self.get_logger().error(f"Error processing action: Missing expected key {e}")
        except IndexError:
            self.get_logger().error("Error processing action: Index out of bounds (check action dimensions).")
        except TypeError as e:
            self.get_logger().error(f"Error processing action: Type mismatch ({e}).")
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
