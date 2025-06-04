#!/usr/bin/env python3
import time
import threading
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS
from brain_messages.action import ExecuteBehavior  # You'll need to create this action
from rclpy.action import ActionServer, CancelResponse
from cv_bridge import CvBridge
import numpy as np
import torch
import os
import cv2
from geometry_msgs.msg import Twist
import yaml
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.executors import MultiThreadedExecutor
import h5py  # Add this import at the top

# Enable CUDNN for better performance
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

# Import your policy class and trajectory generator
from act_test.ACT import ACTPolicy, ACTConfig

def create_act_config():
    """Create ACT configuration matching the training setup."""
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
        chunk_size=30,
        n_action_steps=30,
        input_shapes=input_shapes,
        output_shapes=output_shapes,
        
        # Architecture parameters
        vision_backbone="resnet18",
        replace_final_stride_with_dilation=False,
        
        # Transformer parameters
        pre_norm=False,
        dim_model=512,
        n_heads=8,
        dim_feedforward=3200,
        n_encoder_layers=4,
        n_decoder_layers=4,
        
        # VAE parameters
        use_vae=True,
        
        # Training parameters
        dropout=0.1,
        kl_weight=10.0,
        
        # Temporal ensembling
        temporal_ensemble_coeff=None,
        
        # Optimizer settings
        optimizer_lr=1e-5,
        optimizer_weight_decay=1e-4,
        optimizer_lr_backbone=1e-5,
    )

class BehaviorServer(Node):
    def __init__(self):
        super().__init__('behavior_server')
        self.get_logger().set_level(rclpy.logging.LoggingSeverity.DEBUG)
        self.get_logger().info("Behavior server started.")
        
        self.bridge = CvBridge()
        self.image_size = (640, 480)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        # Load behavior configurations
        self.behaviors_config = self._load_behaviors_config()
        
        # Print all available behaviors at startup
        if self.behaviors_config:
            behavior_names = list(self.behaviors_config.keys())
            self.get_logger().info(f"Available behaviors at startup: {behavior_names}")
            for name, config in self.behaviors_config.items():
                behavior_type = config.get('type', 'unknown')
                self.get_logger().info(f"  - {name} (type: {behavior_type})")
        else:
            self.get_logger().warn("No behaviors loaded!")
        
        # Current execution state
        self.execution_running = False
        self.current_goal_handle = None
        self.current_policy = None
        self._cancel_requested = threading.Event()
        
        # Sensor data
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None
        self.latest_image1_timestamp = None
        self.latest_image2_timestamp = None  
        self.latest_joint_timestamp = None
        
        # Set up QoS profiles
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Subscribers
        self.create_subscription(Image, '/color/image', self.image1_callback, image_qos)
        self.create_subscription(Image, '/image_raw', self.image2_callback, image_qos)
        self.create_subscription(JointState, '/maurice_arm/state', self.joint_state_callback, 10)
        
        # Publishers
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)
        
        # Service client
        self.arm_goto_client = self.create_client(GotoJS, '/maurice_arm/goto_js')
        
        # Action server
        self.action_server = ActionServer(
            self,
            ExecuteBehavior,
            '/behavior/execute',
            execute_callback=self.execute_behavior_callback,
            cancel_callback=self.cancel_behavior_callback
        )
        
        self.get_logger().info(f"Behavior server ready. Available behaviors: {list(self.behaviors_config.keys())}")
    
    def _load_behaviors_config(self):
        """Load behaviors configuration from YAML file."""
        # Get config file path from parameter or use default
        self.declare_parameter('config_file', '')
        config_file_param = self.get_parameter('config_file').value
        
        if config_file_param:
            config_path = config_file_param
        else:
            config_path = os.path.join(
                os.path.dirname(__file__), 
                '../config/behaviors.yaml'
            )
        
        try:
            with open(config_path, 'r') as file:
                config = yaml.safe_load(file)
                behaviors = {}
                
                behavior_names = config['behavior_manager'].get('behavior_names', [])
                for behavior_name in behavior_names:
                    behavior_config = config['behavior_manager'][behavior_name].copy()
                    behavior_config['name'] = behavior_name
                    
                    # Convert poses structure if it exists
                    if 'poses' in behavior_config and isinstance(behavior_config['poses'], dict):
                        poses = []
                        for key in sorted(behavior_config['poses'].keys()):
                            poses.append(behavior_config['poses'][key])
                        behavior_config['poses'] = poses
                    
                    behaviors[behavior_name] = behavior_config
                
                self.get_logger().info(f"Loaded {len(behaviors)} behavior configurations from {config_path}")
                return behaviors
                
        except Exception as e:
            self.get_logger().error(f"Failed to load behaviors config from {config_path}: {e}")
            return {}
    
    def cancel_behavior_callback(self, goal_handle_to_cancel):
        """Handle action cancel requests."""
        self.get_logger().info("Received cancel request...")
        
        if not self.execution_running or not self.current_goal_handle:
            self.get_logger().warn("Rejecting cancel request. No behavior running.")
            return CancelResponse.REJECT
        
        self.get_logger().info("Accepting cancel request.")
        self._cancel_requested.set()
        return CancelResponse.ACCEPT
    
    def execute_behavior_callback(self, goal_handle):
        """Execute the requested behavior."""
        behavior_name = goal_handle.request.behavior_name
        
        if self.execution_running:
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = "Another behavior is already running"
            self.get_logger().warn(f"Behavior {behavior_name} requested but already running")
            goal_handle.abort()
            return result
        
        if behavior_name not in self.behaviors_config:
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = f"Unknown behavior: {behavior_name}"
            self.get_logger().error(f"Unknown behavior requested: {behavior_name}")
            goal_handle.abort()
            return result
        
        # Reset cancel flag
        self._cancel_requested.clear()
        
        # Execute the behavior
        behavior_config = self.behaviors_config[behavior_name]
        outcome = self._execute_behavior(goal_handle, behavior_name, behavior_config)
        
        # Set result based on outcome
        result = ExecuteBehavior.Result()
        
        if outcome == "SUCCESS":
            result.success = True
            result.message = f"Behavior {behavior_name} completed successfully"
            goal_handle.succeed()
            self.get_logger().info(f"Behavior {behavior_name} succeeded")
        elif outcome == "CANCELLED":
            result.success = False
            result.message = f"Behavior {behavior_name} was cancelled"
            goal_handle.canceled()
            self.get_logger().info(f"Behavior {behavior_name} was canceled")
        elif outcome == "FAILURE":
            result.success = False
            result.message = f"Behavior {behavior_name} failed"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {behavior_name} failed")
        else:
            result.success = False
            result.message = f"Behavior {behavior_name} failed with unexpected outcome"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {behavior_name} failed with unexpected outcome: {outcome}")
        
        return result
    
    def _execute_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a behavior based on its configuration."""
        try:
            self.current_goal_handle = goal_handle
            self.execution_running = True
            
            behavior_type = behavior_config.get('type', 'unknown')
            self.get_logger().info(f"Executing {behavior_type} behavior: {behavior_name}")
            
            if behavior_type == 'learned':
                return self._execute_learned_behavior(goal_handle, behavior_name, behavior_config)
            elif behavior_type == 'poses':
                return self._execute_poses_behavior(goal_handle, behavior_name, behavior_config)
            elif behavior_type == 'replay':
                return self._execute_replay_behavior(goal_handle, behavior_name, behavior_config)
            else:
                self.get_logger().error(f"Unknown behavior type: {behavior_type}")
                return "FAILURE"
                
        except Exception as e:
            self.get_logger().error(f"Error executing behavior {behavior_name}: {e}")
            return "FAILURE"
        finally:
            # Clean up
            self.execution_running = False
            self.current_goal_handle = None
            if self.current_policy:
                del self.current_policy
                self.current_policy = None
    
    def _execute_learned_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a learned behavior using ACT policy."""
        try:
            # Load the policy for this behavior
            checkpoint_path = behavior_config.get('file_path')
            checkpoint_path = os.path.expanduser(checkpoint_path)
            if not checkpoint_path or not os.path.exists(checkpoint_path):
                self.get_logger().error(f"Checkpoint not found: {checkpoint_path}")
                return "FAILURE"
            
            # Load policy
            if not self._load_policy_for_behavior(checkpoint_path):
                return "FAILURE"
            
            # Get behavior parameters
            duration = behavior_config.get('duration', 20.0)
            start_pose = behavior_config.get('start_pose')
            end_pose = behavior_config.get('end_pose')
            
            # Move to start pose if specified
            if start_pose:
                self.get_logger().info(f"Moving to start pose: {start_pose}")
                if not self.call_arm_goto_service(start_pose, 5):
                    self.get_logger().error("Failed to move to start pose")
                    return "FAILURE"
                time.sleep(5.0)
            
            # Execute policy inference
            start_time = time.time()
            inference_hz = 25.0
            period = 1.0 / inference_hz
            
            self.get_logger().info(f"Starting policy inference for {duration} seconds")
            
            while rclpy.ok():
                loop_start = time.time()
                
                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Behavior execution canceled")
                    self._stop_robot()
                    if end_pose:
                        self.call_arm_goto_service(end_pose, 3)
                    return "CANCELLED"
                
                # Run inference
                self._run_inference_once()
                
                # Send feedback
                elapsed_time = loop_start - start_time
                remaining_time = max(0.0, duration - elapsed_time)
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                feedback_msg.status = f"Executing {behavior_name}"
                goal_handle.publish_feedback(feedback_msg)
                
                # Check if duration completed
                if elapsed_time >= duration:
                    break
                
                # Maintain loop rate
                sleep_time = period - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            # Stop robot and move to end pose
            self._stop_robot()
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose}")
                if not self.call_arm_goto_service(end_pose, 5):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE"
            
            self.get_logger().info(f"Learned behavior {behavior_name} completed successfully")
            return "SUCCESS"
            
        except Exception as e:
            self.get_logger().error(f"Error in learned behavior execution: {e}")
            self._stop_robot()
            return "FAILURE"
    
    def _execute_poses_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a poses-based behavior (placeholder)."""
        self.get_logger().info(f"Executing poses behavior: {behavior_name}")
        
        # Placeholder implementation
        poses = behavior_config.get('poses', [])
        steps = int(behavior_config.get('steps', len(poses)))  # Convert to int here
        
        try:
            for i, pose in enumerate(poses):
                # Check for cancellation
                if self._cancel_requested.is_set():
                    return "CANCELLED"
                
                self.get_logger().info(f"Moving to pose {i+1}/{len(poses)}: {pose}")
                
                # Send feedback
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(i * steps)
                feedback_msg.remaining_time = float((len(poses) - i - 1) * steps)
                feedback_msg.status = f"Executing pose {i+1}/{len(poses)}"
                goal_handle.publish_feedback(feedback_msg)
                
                # Execute pose movement
                if not self.call_arm_goto_service(pose, steps):
                    self.get_logger().error(f"Failed to reach pose {i+1}")
                    return "FAILURE"
                
                time.sleep(steps)
            
            self.get_logger().info(f"Poses behavior {behavior_name} completed successfully")
            return "SUCCESS"
            
        except Exception as e:
            self.get_logger().error(f"Error in poses behavior execution: {e}")
            return "FAILURE"
    
    def _execute_replay_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a replay-based behavior from H5 file."""
        self.get_logger().info(f"Executing replay behavior: {behavior_name}")
        
        file_path = behavior_config.get('file_path')
        file_path = os.path.expanduser(file_path)
        end_pose = behavior_config.get('end_pose')
        
        if not file_path or not os.path.exists(file_path):
            self.get_logger().error(f"Replay file not found: {file_path}")
            return "FAILURE"
        
        try:
            # Load H5 file and extract actions
            with h5py.File(file_path, 'r') as h5file:
                if 'action' not in h5file:
                    self.get_logger().error("No 'action' dataset found in H5 file")
                    return "FAILURE"
                
                actions = h5file['action'][:]  # Shape: (n_steps, 8)
                self.get_logger().info(f"Loaded {actions.shape[0]} action steps from {file_path}")
            
            if actions.shape[0] == 0:
                self.get_logger().error("No actions found in replay file")
                return "FAILURE"
            
            # Extract first action and arm position
            first_action = actions[0]  # Shape: (8,)
            first_arm_position = first_action[0:6].tolist()  # Convert to list for service call
            
            self.get_logger().info(f"Moving to initial arm position: {first_arm_position}")
            
            # Move to initial arm position
            if not self.call_arm_goto_service(first_arm_position, 5):
                self.get_logger().error("Failed to move to initial arm position")
                return "FAILURE"
            
            # Wait for arm movement to complete
            time.sleep(5.0)
            
            # Replay parameters
            total_steps = actions.shape[0]
            replay_hz = 25.0  # Match the frequency used in learned behaviors
            step_duration = 1.0 / replay_hz
            total_duration = total_steps * step_duration
            
            self.get_logger().info(f"Starting replay: {total_steps} steps at {replay_hz} Hz (total: {total_duration:.1f}s)")
            
            # Execute replay
            start_time = time.time()
            
            for step_idx in range(total_steps):
                loop_start = time.time()
                
                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Replay execution canceled")
                    self._stop_robot()
                    if end_pose:
                        self.call_arm_goto_service(end_pose, 3)
                    return "CANCELLED"
                
                # Get current action
                action = actions[step_idx]  # Shape: (8,)
                
                # Extract arm commands (first 6 elements)
                arm_msg = Float64MultiArray()
                arm_msg.data = [float(v) for v in action[0:6]]
                self.arm_state_pub.publish(arm_msg)
                
                # Extract cmd_vel commands (last 2 elements)
                twist_msg = Twist()
                twist_msg.linear.x = float(action[6])
                twist_msg.angular.z = float(action[7])
                self.cmd_vel_pub.publish(twist_msg)
                
                # Send feedback
                elapsed_time = loop_start - start_time
                remaining_time = max(0.0, total_duration - elapsed_time)
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                feedback_msg.status = f"Replaying step {step_idx+1}/{total_steps}"
                goal_handle.publish_feedback(feedback_msg)
                
                # Maintain replay frequency
                sleep_time = step_duration - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            # Stop robot after replay
            self._stop_robot()
            
            # Move to end pose if specified
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose}")
                if not self.call_arm_goto_service(end_pose, 5):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE"
            
            self.get_logger().info(f"Replay behavior {behavior_name} completed successfully")
            return "SUCCESS"
            
        except Exception as e:
            self.get_logger().error(f"Error in replay behavior execution: {e}")
            self._stop_robot()
            return "FAILURE"
    
    def _load_policy_for_behavior(self, checkpoint_path):
        """Load ACT policy for a specific behavior."""
        try:
            # Clean up previous policy
            if self.current_policy:
                del self.current_policy
            
            # Expand user path
            checkpoint_path = os.path.expanduser(checkpoint_path)
            
            # Load normalization stats
            checkpoint_dir = os.path.dirname(checkpoint_path)
            stats_path = os.path.join(checkpoint_dir, 'dataset_stats.pt')
            
            dataset_stats = None
            try:
                dataset_stats = torch.load(stats_path, map_location='cpu')
                self.get_logger().info("Dataset stats loaded")
            except Exception as e:
                self.get_logger().warn(f"Could not load dataset stats: {e}")
            
            # Create and load policy
            policy_config = create_act_config()
            self.current_policy = ACTPolicy(config=policy_config, dataset_stats=dataset_stats).to(self.device)
            
            state_dict = torch.load(checkpoint_path, map_location=self.device)
            self.current_policy.load_state_dict(state_dict)
            self.current_policy.eval()
            
            # Reset policy to clear any cached states
            self.current_policy.reset()
            
            self.get_logger().info(f"Policy loaded successfully from {checkpoint_path}")
            return True
            
        except Exception as e:
            self.get_logger().error(f"Failed to load policy: {e}")
            return False
    
    # ... existing sensor callbacks and utility methods from policy.py ...
    def image1_callback(self, msg: Image):
        try:
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image1_timestamp = msg.header.stamp
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        try:
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image2_timestamp = msg.header.stamp
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        self.latest_joint_state = msg
        self.latest_joint_timestamp = msg.header.stamp

    def _run_inference_once(self):
        """Run one inference step with current policy."""
        if not self.current_policy or self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            return

        try:
            # Image preprocessing
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

            # Joint state preprocessing
            qpos = np.asarray(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos, device=self.device).unsqueeze(0)

            batch = {
                "observation.image_camera_1": img1_tensor,
                "observation.image_camera_2": img2_tensor,
                "observation.state": qpos_tensor
            }

            # Policy forward pass
            with torch.no_grad():
                action = self.current_policy.select_action(batch)
                action_np = action.cpu().numpy().squeeze(0)

                if action_np.shape[0] < 8:
                    self.get_logger().error(f"Action has wrong dimensions. Expected 8, got {action_np.shape[0]}")
                    return

                # Publish commands
                twist_msg = Twist()
                twist_msg.linear.x = float(action_np[-2]) / 2
                twist_msg.angular.z = float(action_np[-1]) / 2
                self.cmd_vel_pub.publish(twist_msg)

                arm_msg = Float64MultiArray()
                arm_msg.data = [float(v) for v in action_np[:6]]
                self.arm_state_pub.publish(arm_msg)

        except Exception as e:
            self.get_logger().error(f"Error during inference: {e}")

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

def main(args=None):
    rclpy.init(args=args)
    node = BehaviorServer()
    
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Behavior server shutting down.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
