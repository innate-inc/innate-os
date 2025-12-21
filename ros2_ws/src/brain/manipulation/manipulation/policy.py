#!/usr/bin/env python3
import time
import threading
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray  # For arm commands
from std_srvs.srv import Trigger  # Simple service
from maurice_msgs.srv import GotoJS  # Add this import for arm service
from brain_messages.action import ExecutePolicy  # Add this import for action
from rclpy.action import ActionServer, CancelResponse
from cv_bridge import CvBridge
import numpy as np
import torch
import os
import cv2
from geometry_msgs.msg import Twist
import time
import threading
import torch.amp
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.executors import MultiThreadedExecutor

# Enable CUDNN for better performance
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

# Import your policy class and trajectory generator.
from manipulation.ACT import ACTPolicy, ACTConfig

# Convert old policy_config to new ACTConfig format
def create_act_config():
    # Define input shapes based on camera setup and state/action dimensions
    input_shapes = {
        "observation.image_camera_1": [3, 480, 640],  # [C, H, W]
        "observation.image_camera_2": [3, 480, 640],  # [C, H, W]
        "observation.state": [6]  # state_dim
    }
    
    output_shapes = {
        "action": [8]  # action_dim
    }
    
    return ACTConfig(
        n_obs_steps=1,
        chunk_size=30,  # num_queries
        n_action_steps=30,  # CHANGED: Match training config for efficiency
        input_shapes=input_shapes,
        output_shapes=output_shapes,
        
        # Architecture parameters - MATCH TRAINING
        vision_backbone="resnet18",  # backbone
        replace_final_stride_with_dilation=False,  # not dilation
        
        # Transformer parameters - MATCH TRAINING
        pre_norm=False,
        dim_model=512,  # hidden_dim
        n_heads=8,  # nheads
        dim_feedforward=3200,
        n_encoder_layers=4,  # enc_layers
        n_decoder_layers=4,  # CHANGED: Match train.py (was 7)
        
        # VAE parameters - MATCH TRAINING
        use_vae=True,
        
        # Training parameters - MATCH TRAINING
        dropout=0.1,
        kl_weight=10.0,  # CHANGED: Match train.py (was 100)
        
        # Temporal ensembling (enable if desired)
        temporal_ensemble_coeff=None,  # CHANGED: Match train.py default (was 0.3)
        
        # Optimizer settings - MATCH TRAINING
        optimizer_lr=1e-5,  # CHANGED: Match train.py (was 5e-5)
        optimizer_weight_decay=1e-4,  # weight_decay
        optimizer_lr_backbone=1e-5,  # CHANGED: Match train.py (was 5e-5)
    )

# Global configuration
policy_config = create_act_config()

class InferenceNode(Node):
    def __init__(self):
        super().__init__('inference_node')
        # Enable debug logging
        self.get_logger().set_level(rclpy.logging.LoggingSeverity.DEBUG)
        self.get_logger().info("Inference node started.")
        self.bridge = CvBridge()
        self.image_size = (640, 480)

        # Set device and load the policy model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        # Load normalization stats first
        # Use package-relative path for checkpoint
        # Get the manipulation package directory (source, not installed)
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        manipulation_pkg_dir = os.path.join(maurice_root, 'ros2_ws', 'src', 'brain', 'manipulation')
        checkpoint_path = os.path.join(manipulation_pkg_dir, 'ckpts', 'PaperCorner_Filtered_20250526_213031', 'act_policy_epoch_90000.pth')
        checkpoint_dir = os.path.dirname(checkpoint_path)
        stats_path = os.path.join(checkpoint_dir, 'dataset_stats.pt')
        
        # Load dataset stats for normalization
        dataset_stats = None
        try:
            # Load from .pt file instead of .pkl
            dataset_stats = torch.load(stats_path, map_location='cpu')
            self.get_logger().info("Dataset stats loaded from .pt file.")
        except Exception as e:
            self.get_logger().error(f"Failed to load dataset stats: {e}")
            dataset_stats = None
        
        # Initialize policy with config and stats
        self.policy = ACTPolicy(config=policy_config, dataset_stats=dataset_stats).to(self.device)
        
        try:
            state_dict = torch.load(checkpoint_path, map_location=self.device)
            self.policy.load_state_dict(state_dict)
            self.policy.eval()
            self.get_logger().info("Policy loaded successfully.")
        except Exception as e:
            self.get_logger().error(f"Failed to load policy checkpoint: {e}")

        # Warm up the model with dummy forward passes
        self._warmup_model()

        # Set up sensor QoS profile
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        # Variables to hold the latest sensor data
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None
        # Add timestamp tracking
        self.latest_image1_timestamp = None
        self.latest_image2_timestamp = None  
        self.latest_joint_timestamp = None

        # Inference control variables
        self.inference_running = False
        self.inference_start_time = None
        self.current_goal_handle = None  # Track current action goal
        
        # Arm positions for start and end
        self.start_position = [0.143, -0.434, 0.147, 0.788, 0.060, 0.701]
        self.end_position = [0.853, -0.457, 1.295, -0.933, -0.049, 0.0]
        
        # Subscribers for images and joint state with sensor QoS
        self.create_subscription(Image, '/mars/main_camera/image', self.image1_callback, image_qos)
        self.create_subscription(Image, '/mars/arm/image_raw', self.image2_callback, image_qos)
        self.create_subscription(JointState, '/mars/arm/state', self.joint_state_callback, 10)

        # Publishers for twist and arm commands
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/mars/arm/commands', 10)

        # Service client for arm positioning
        self.arm_goto_client = self.create_client(GotoJS, '/mars/arm/goto_js')
        
        # Action server for policy execution (replaces service server)
        self.action_server = ActionServer(
            self,
            ExecutePolicy,
            '/policy/execute',
            execute_callback=self.execute_policy_callback,
            cancel_callback=self.cancel_policy_callback
        )
        self._cancel_requested = threading.Event()
        
        # Desired inference frequency (Hz). The policy loop now lives
        # directly in the action thread instead of a separate timer.
        self.inference_hz = 25.0
        
        self.get_logger().info("Policy loaded and ready. Call '/policy/execute' action to start inference.")

    def cancel_policy_callback(self, goal_handle_to_cancel):
        """Handle action cancel requests."""
        self.get_logger().info(f"Received cancel request...")

        # Check if this is the currently active goal
        if not self.inference_running or not self.current_goal_handle:
            self.get_logger().warn(
                f"Rejecting cancel request. "
                f"Policy not running. "
            )
            return CancelResponse.REJECT

        # If it is the active goal, accept the cancellation.
        # The execution loop (_execute_complete_policy) will see _cancel_requested
        # and handle the cleanup and state transition.
        self.get_logger().info(f"Accepting cancel request.")
        self._cancel_requested.set()
        return CancelResponse.ACCEPT
        
    def execute_policy_callback(self, goal_handle):
        """Action callback to execute complete policy workflow."""
        if self.inference_running:
            result = ExecutePolicy.Result()
            result.success = False
            self.get_logger().warn("Policy execution requested but already running")
            goal_handle.abort()
            return result
        
        # Reset the cancel flag
        self._cancel_requested.clear()
        
        # Execute the complete policy workflow
        outcome = self._execute_complete_policy(goal_handle)
        
        # Set the goal state and return result
        result = ExecutePolicy.Result()
        
        if outcome == "SUCCESS":
            result.success = True
            goal_handle.succeed()
            self.get_logger().info("Policy execution succeeded")
        elif outcome == "CANCELLED":
            result.success = False # Cancelled is not a success of the primary goal
            goal_handle.canceled()
            self.get_logger().info("Policy execution was canceled by request")
        elif outcome == "FAILURE":
            result.success = False
            goal_handle.abort()
            self.get_logger().error("Policy execution failed")
        else: # Should not happen with defined string outcomes
            result.success = False
            goal_handle.abort()
            self.get_logger().error(f"Policy execution failed with unexpected outcome: {outcome}")
            
        return result

    def _execute_complete_policy(self, goal_handle):
        """Execute complete policy workflow: arm move -> inference -> arm move.
        Returns:
            str: "SUCCESS", "FAILURE", or "CANCELLED"
        """
        try:
            # Set up execution state
            self.current_goal_handle = goal_handle
            self.inference_running = True
            
            # Reset policy to clear any queues/buffers from previous runs
            self.policy.reset()
            self.get_logger().info("Policy reset completed - starting fresh execution")
            
            # Get inference duration from the goal
            inference_duration = goal_handle.request.inference_duration
            if inference_duration <= 0:
                self.get_logger().warn(
                    f"Invalid inference duration {inference_duration}s requested, using default 20s."
                )
                inference_duration = 20.0 # Default if not specified or invalid

            # Step 1: Move arm to start position
            self.get_logger().info("Moving arm to start position...")
            if not self.call_arm_goto_service(self.start_position, 5):
                self.get_logger().error("Failed to move arm to start position")
                return "FAILURE"
                
            # Wait for arm movement to complete
            time.sleep(5.0)
            
            # Step 2: Run inference for specified duration
            self.inference_start_time = time.time()
            self.get_logger().info(
                f"Policy inference started for {inference_duration} seconds "
                f"at ≈{self.inference_hz} Hz"
            )

            period = 1.0 / self.inference_hz
            while rclpy.ok():
                loop_start = time.time()

                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Policy execution canceled by request")
                    self._stop_robot()
                    self.call_arm_goto_service(self.end_position, 3) # Attempt cleanup move
                    return "CANCELLED"

                # One inference step
                self._run_inference_once()

                # Feedback
                elapsed_time = loop_start - self.inference_start_time
                remaining_time = max(0.0, inference_duration - elapsed_time)
                feedback_msg = ExecutePolicy.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                goal_handle.publish_feedback(feedback_msg)

                # Stop after the allotted duration
                if elapsed_time >= inference_duration:
                    break

                # Sleep to maintain the desired loop rate
                sleep_time = period - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            # Step 3: Stop robot and move arm to end position
            self._stop_robot()
            self.get_logger().info("Moving arm to end position...")
            if not self.call_arm_goto_service(self.end_position, 5):
                self.get_logger().error("Failed to move arm to end position after inference.")
                return "FAILURE"
            
            self.get_logger().info("Policy execution completed successfully")
            return "SUCCESS"
            
        except Exception as e:
            self.get_logger().error(f"Error during policy execution: {e}")
            self._stop_robot()
            return "FAILURE"
            
        finally:
            # Clean up state
            self.inference_running = False
            self.current_goal_handle = None

    def _stop_robot(self):
        """Stop the robot by sending zero commands."""
        twist_msg = Twist()
        twist_msg.linear.x = 0.0
        twist_msg.angular.z = 0.0
        self.cmd_vel_pub.publish(twist_msg)

    def call_arm_goto_service(self, position, time_duration=5):
        """Call the arm goto service with specified position."""
        if not self.arm_goto_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error("Arm goto service not available")
            return False
            
        request = GotoJS.Request()
        request.data.data = position
        request.time = time_duration
        
        try:
            future = self.arm_goto_client.call_async(request)
            self.get_logger().info(f"Arm goto service called with position: {position}")
            return True
                
        except Exception as e:
            self.get_logger().error(f"Error calling arm goto service: {e}")
            return False

    ####################################################
    # Callback Methods for Sensor Data
    ####################################################
    def image1_callback(self, msg: Image):
        try:
            # Specify encoding explicitly instead of passthrough
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image1_timestamp = msg.header.stamp
            # self.get_logger().debug(f"Received image1 at {msg.header.stamp.sec}.{msg.header.stamp.nanosec}")
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        try:
            # Specify encoding explicitly instead of passthrough  
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image2_timestamp = msg.header.stamp
            # self.get_logger().debug(f"Received image2 at {msg.header.stamp.sec}.{msg.header.stamp.nanosec}")
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        self.latest_joint_state = msg
        self.latest_joint_timestamp = msg.header.stamp
        # self.get_logger().debug(f"Received joint state at {msg.header.stamp.sec}.{msg.header.stamp.nanosec}")

    ####################################################
    # Single-step Inference (called from the action loop)
    ####################################################
    def _run_inference_once(self):
        """
        Run one forward pass of the policy and publish the resulting
        base and arm commands. Returns immediately if any sensor data
        is still missing.
        """
        if self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            # self.get_logger().warn("Missing sensor data - skipping inference")
            return

        # Check how recent the data is (commented out for now)
        # current_time = self.get_clock().now()
        # if self.latest_image1_timestamp:
        #     img1_age = (current_time - self.latest_image1_timestamp).nanoseconds / 1e9
        #     self.get_logger().debug(f"Image1 age: {img1_age:.3f}s")
        # if self.latest_image2_timestamp:
        #     img2_age = (current_time - self.latest_image2_timestamp).nanoseconds / 1e9  
        #     self.get_logger().debug(f"Image2 age: {img2_age:.3f}s")

        # --- image preprocessing ---
        try:
            # Convert from BGR to RGB if needed (OpenCV uses BGR, neural nets often expect RGB)
            img1_rgb = cv2.cvtColor(self.latest_image1, cv2.COLOR_BGR2RGB)
            img2_rgb = cv2.cvtColor(self.latest_image2, cv2.COLOR_BGR2RGB)
            
            img1 = cv2.resize(img1_rgb, self.image_size)
            img2 = cv2.resize(img2_rgb, self.image_size)
            img1 = img1.astype(np.float32) / 255.0
            img2 = img2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
            img1_tensor = torch.tensor(img1, device=self.device).unsqueeze(0)
            img2_tensor = torch.tensor(img2, device=self.device).unsqueeze(0)
            
            # self.get_logger().debug(f"Processed images - img1 shape: {img1_tensor.shape}, img2 shape: {img2_tensor.shape}")
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
            return

        # --- joint-state preprocessing ---
        try:
            qpos = np.asarray(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos, device=self.device).unsqueeze(0)
            # self.get_logger().debug(f"Joint state: {qpos}")
        except Exception as e:
            self.get_logger().error(f"Error processing joint state: {e}")
            return

        batch = {
            "observation.image_camera_1": img1_tensor,
            "observation.image_camera_2": img2_tensor,
            "observation.state": qpos_tensor
        }

        # --- policy forward pass ---
        with torch.no_grad():
            try:
                action = self.policy.select_action(batch)
                action_np = action.cpu().numpy().squeeze(0)

                if action_np.shape[0] < 8:
                    self.get_logger().error(
                        f"Action has wrong dimensions. Expected 8, got {action_np.shape[0]}"
                    )
                    return

                # Base command – last two values
                twist_msg = Twist()
                twist_msg.linear.x = float(action_np[-2]) / 2
                twist_msg.angular.z = float(action_np[-1]) / 2
                self.cmd_vel_pub.publish(twist_msg)

                # Arm command – first six values
                arm_msg = Float64MultiArray()
                arm_msg.data = [float(v) for v in action_np[:6]]
                self.arm_state_pub.publish(arm_msg)

                # Reduce this to just basic info
                self.get_logger().info(
                    f"Published cmds – twist: [{twist_msg.linear.x:.3f}, {twist_msg.angular.z:.3f}], "
                    f"arm: {arm_msg.data[:3]}..."
                )
            except Exception as e:
                self.get_logger().error(f"Error during inference: {e}")

    def _warmup_model(self):
        """Warm up the model with 3 forward passes using dummy data."""
        self.get_logger().info("Warming up model with dummy forward passes...")
        
        try:
            # Create dummy data matching expected input shapes
            dummy_batch = {
                "observation.image_camera_1": torch.randn(1, 3, 480, 640, device=self.device, dtype=torch.float32),
                "observation.image_camera_2": torch.randn(1, 3, 480, 640, device=self.device, dtype=torch.float32),
                "observation.state": torch.randn(1, 6, device=self.device, dtype=torch.float32)
            }
            
            # Perform 3 warmup forward passes
            with torch.no_grad():
                for i in range(3):
                    self.get_logger().info(f"Warmup pass {i+1}/3...")
                    # Reset the policy to clear the action queue before each forward pass
                    self.policy.reset()
                    # This will now trigger a forward pass since the queue is empty
                    _ = self.policy.select_action(dummy_batch)
                    # Small delay to ensure GPU operations complete
                    time.sleep(0.1)
            
            # Reset one final time to start fresh for actual inference
            self.policy.reset()
            self.get_logger().info("Model warmup completed successfully.")
            
        except Exception as e:
            self.get_logger().error(f"Error during model warmup: {e}")
            # Continue anyway - warmup failure shouldn't prevent operation

def main(args=None):
    rclpy.init(args=args)
    node = InferenceNode()
    
    # Use MultiThreadedExecutor instead of default single-threaded
    executor = MultiThreadedExecutor(num_threads=4)  # Adjust thread count as needed
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Inference node shutting down.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
