#!/usr/bin/env python3
import time
import threading
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray
from std_srvs.srv import Trigger
from maurice_msgs.srv import GotoJS
from brain_messages.action import ExecuteBehavior
from rclpy.action import ActionServer, CancelResponse
from cv_bridge import CvBridge
import numpy as np
import torch
import os
import cv2
from geometry_msgs.msg import Twist
import json
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.executors import MultiThreadedExecutor
import h5py

# Enable CUDNN for better performance
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

# Import your policy class and trajectory generator
from manipulation.ACT import ACTPolicy, ACTConfig

def create_act_config(action_dim=8):
    """Create ACT configuration matching the training setup."""
    input_shapes = {
        "observation.image_camera_1": [3, 224, 224],  # [C, H, W]
        "observation.image_camera_2": [3, 224, 224],  # [C, H, W]
        "observation.state": [6]  # state_dim
    }
    
    output_shapes = {
        "action": [action_dim]  # action_dim - now configurable
    }
    
    return ACTConfig(
        n_obs_steps=1,
        chunk_size=30,
        n_action_steps=10,
        speed=1.5,
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
        
        # Use environment variable if set, otherwise construct from HOME
        maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        
        # Primitives directory for loading checkpoints/data
        self.primitives_directory = os.path.join(maurice_root, 'skills')
        
        # Get data directory from recorder config
        default_data_dir = os.path.join(maurice_root, 'data')
        try:
            self.declare_parameter('data_directory', default_data_dir)
            self.data_directory = os.path.expanduser(self.get_parameter('data_directory').value)
            self.get_logger().info(f"Data directory: {self.data_directory}")
        except Exception as e:
            self.get_logger().warn(f"Could not load data_directory parameter: {e}")
            self.data_directory = default_data_dir
        
        # Image size for policy inference (matches checkpoint training)
        self.bridge = CvBridge()
        self.image_size = (224, 224)  # Resize to match checkpoint expectations
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        
        # Log device info
        self.get_logger().info(f"PyTorch device: {self.device}")
        self.get_logger().info(f"CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            self.get_logger().info(f"CUDA device: {torch.cuda.get_device_name(0)}")
            self.get_logger().info(f"CUDA memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
        
        # Current execution state
        self.execution_running = False
        self.current_goal_handle = None
        self.current_policy = None
        self.current_action_dim = 8  # Add this to track current policy's action dimension
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
        self.create_subscription(Image, '/mars/main_camera/left/image_raw', self.image1_callback, image_qos)
        self.create_subscription(Image, '/mars/arm/image_raw', self.image2_callback, image_qos)
        self.create_subscription(JointState, '/mars/arm/state', self.joint_state_callback, 10)
        
        # Publishers
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/mars/arm/commands', 10)
        # Service clients
        self.head_ai_position_client = self.create_client(Trigger, '/mars/head/set_ai_position')
        self.arm_goto_client = self.create_client(GotoJS, '/mars/arm/goto_js')
        
        # Action server
        self.action_server = ActionServer(
            self,
            ExecuteBehavior,
            '/behavior/execute',
            execute_callback=self.execute_behavior_callback,
            cancel_callback=self.cancel_behavior_callback
        )
        
        self.get_logger().info("Behavior server ready - pure execution engine using primitives/ directory")
    
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
        
        # Get config from goal (from primitive_execution_action_server)
        if not hasattr(goal_handle.request, 'behavior_config') or not goal_handle.request.behavior_config:
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = f"No behavior_config provided for behavior: {behavior_name}"
            self.get_logger().error(f"No behavior_config provided for behavior: {behavior_name}")
            goal_handle.abort()
            return result
        
        try:
            behavior_config = json.loads(goal_handle.request.behavior_config)
            self.get_logger().info(f"Executing behavior: {behavior_name}")
        except json.JSONDecodeError as e:
            self.get_logger().error(f"Failed to parse behavior_config JSON: {e}")
            result = ExecuteBehavior.Result()
            result.success = False
            result.message = f"Invalid behavior_config JSON: {e}"
            goal_handle.abort()
            return result
        
        # Reset cancel flag
        self._cancel_requested.clear()
        
        # Execute the behavior
        outcome, reason = self._execute_behavior(goal_handle, behavior_name, behavior_config)
        
        # Set result based on outcome
        result = ExecuteBehavior.Result()
        
        if outcome == "SUCCESS":
            result.success = True
            result.message = f"Behavior {behavior_name} completed successfully. {reason}"
            goal_handle.succeed()
            self.get_logger().info(f"Behavior {behavior_name} succeeded: {reason}")
        elif outcome == "CANCELLED":
            result.success = False
            result.message = f"Behavior {behavior_name} was cancelled. {reason}"
            goal_handle.canceled()
            self.get_logger().info(f"Behavior {behavior_name} was canceled: {reason}")
        elif outcome == "FAILURE":
            result.success = False
            result.message = f"Behavior {behavior_name} failed. {reason}"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {behavior_name} failed: {reason}")
        else:
            result.success = False
            result.message = f"Behavior {behavior_name} failed with unexpected outcome: {outcome}. {reason}"
            goal_handle.abort()
            self.get_logger().error(f"Behavior {behavior_name} failed with unexpected outcome: {outcome}. {reason}")
        
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
                return "FAILURE", f"Unknown behavior type: {behavior_type}"
                
        except Exception as e:
            self.get_logger().error(f"Error executing behavior {behavior_name}: {e}")
            return "FAILURE", f"Exception during execution: {str(e)}"
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
            # Load from primitives/{behavior_name}/ directory
            primitive_dir = os.path.join(self.primitives_directory, behavior_name)
            checkpoint_file = behavior_config['execution'].get('checkpoint')
            checkpoint_path = os.path.join(primitive_dir, checkpoint_file)
            action_dim = behavior_config['execution'].get('action_dim', 8)
            duration = behavior_config['execution'].get('duration', 20.0)
            progress_threshold = behavior_config['execution'].get('progress_threshold', 2.0)
            start_pose = behavior_config['execution'].get('start_pose')
            end_pose = behavior_config['execution'].get('end_pose')
            start_pose_time = behavior_config['execution'].get('start_pose_time', 1)
            end_pose_time = behavior_config['execution'].get('end_pose_time', 1)
            
            # Treat empty lists as None
            if start_pose is not None and len(start_pose) == 0:
                start_pose = None
            if end_pose is not None and len(end_pose) == 0:
                end_pose = None
            
            if not checkpoint_path or not os.path.exists(checkpoint_path):
                self.get_logger().error(f"Checkpoint not found: {checkpoint_path}")
                return "FAILURE", f"Checkpoint file not found: {checkpoint_path}"
            
            # Load policy
            if not self._load_policy_for_behavior(checkpoint_path, action_dim):
                return "FAILURE", f"Failed to load policy from {checkpoint_path}"
            
            # Check sensor availability before starting
            if not self._check_sensor_availability():
                self.get_logger().error("Required sensors not available. Cannot execute learned behavior.")
                return "FAILURE", "Required sensors not available (cameras or joint state)"
            
            # Set head to AI position for optimal camera angle
            self.get_logger().info("Setting head to AI position for optimal camera angle")
            self._set_head_ai_position()
            
            # Move to start pose if specified
            if start_pose:
                self.get_logger().info(f"Moving to start pose: {start_pose} (time: {start_pose_time}s)")
                if not self.call_arm_goto_service(start_pose, start_pose_time):
                    self.get_logger().error("Failed to move to start pose")
                    return "FAILURE", "Failed to move arm to start pose"
            
            # Execute policy inference
            start_time = time.time()
            inference_hz = 25.0
            period = 1.0 / inference_hz
            early_termination = False
            
            # Timing metrics for jitter analysis
            loop_times = []
            inference_times = []
            # Detailed timing breakdowns
            preprocess_times = []
            tensor_creation_times = []
            model_inference_times = []
            cpu_transfer_times = []
            publish_times = []
            prev_loop_start = None
            timing_log_interval = 10  # Log stats every N iterations
            iteration_count = 0
            
            self.get_logger().info(f"Starting policy inference for {duration} seconds at {inference_hz} Hz (progress threshold: {progress_threshold})")
            
            while rclpy.ok():
                loop_start = time.time()
                elapsed_time = loop_start - start_time
                iteration_count += 1
                
                # Track loop-to-loop timing (jitter measurement)
                # Measure from start of previous iteration to start of current iteration
                # This captures the full loop period including inference and sleep time
                if prev_loop_start is not None:
                    loop_interval = loop_start - prev_loop_start
                    loop_times.append(loop_interval)
                prev_loop_start = loop_start
                
                # Check for cancellation
                if self._cancel_requested.is_set():
                    self.get_logger().info("Behavior execution canceled")
                    self._stop_robot()
                    if end_pose:
                        self.call_arm_goto_service(end_pose, end_pose_time)
                    return "CANCELLED", "User requested cancellation"
                
                # Check if duration completed (moved earlier to prevent infinite loop)
                if elapsed_time >= duration:
                    self.get_logger().info(f"Behavior timeout reached after {elapsed_time:.2f} seconds")
                    break
                
                # Run inference and get progress value
                inference_start = time.time()
                detailed_timing = {}
                progress = self._run_inference_once(detailed_timing=detailed_timing)
                inference_end = time.time()
                inference_times.append(inference_end - inference_start)
                
                # Store detailed timing breakdowns
                if detailed_timing:
                    preprocess_times.append(detailed_timing.get('preprocess', 0))
                    tensor_creation_times.append(detailed_timing.get('tensor_creation', 0))
                    model_inference_times.append(detailed_timing.get('model_inference', 0))
                    cpu_transfer_times.append(detailed_timing.get('cpu_transfer', 0))
                    publish_times.append(detailed_timing.get('publish', 0))
                
                # Check if inference failed due to missing sensor data
                if progress is None:
                    # Check if it's due to missing sensors
                    if not self._check_sensor_availability():
                        self.get_logger().error("Required sensors became unavailable during execution")
                        self._stop_robot()
                        return "FAILURE", "Required sensors became unavailable during execution"
                    # If sensors are available but inference still failed, continue
                    # but still check timeout and send feedback
                else:
                    # Check for early termination based on progress metric
                    if progress > progress_threshold:
                        early_termination = True
                        self.get_logger().info(f"Early termination triggered! Progress: {progress:.4f} > {progress_threshold}")
                        break
                
                # Log timing stats periodically
                if iteration_count % timing_log_interval == 0 and len(loop_times) > 0:
                    avg_loop = np.mean(loop_times[-timing_log_interval:]) * 1000
                    max_loop = np.max(loop_times[-timing_log_interval:]) * 1000
                    min_loop = np.min(loop_times[-timing_log_interval:]) * 1000
                    std_loop = np.std(loop_times[-timing_log_interval:]) * 1000
                    avg_inference = np.mean(inference_times[-timing_log_interval:]) * 1000
                    max_inference = np.max(inference_times[-timing_log_interval:]) * 1000
                    actual_hz = 1000.0 / avg_loop if avg_loop > 0 else 0
                    
                    self.get_logger().info(
                        f"[TIMING] iter={iteration_count} | "
                        f"loop: avg={avg_loop:.1f}ms, min={min_loop:.1f}ms, max={max_loop:.1f}ms, jitter(std)={std_loop:.2f}ms | "
                        f"inference: avg={avg_inference:.1f}ms, max={max_inference:.1f}ms | "
                        f"actual_hz={actual_hz:.1f} (target={inference_hz})"
                    )
                    
                    # Log detailed inference timing breakdown
                    if (len(preprocess_times) >= timing_log_interval and 
                        len(tensor_creation_times) >= timing_log_interval and
                        len(model_inference_times) >= timing_log_interval):
                        avg_preprocess = np.mean(preprocess_times[-timing_log_interval:]) * 1000
                        avg_tensor = np.mean(tensor_creation_times[-timing_log_interval:]) * 1000
                        avg_model = np.mean(model_inference_times[-timing_log_interval:]) * 1000
                        avg_cpu = np.mean(cpu_transfer_times[-timing_log_interval:]) * 1000
                        avg_publish = np.mean(publish_times[-timing_log_interval:]) * 1000
                        
                        self.get_logger().info(
                            f"[DETAILED TIMING] iter={iteration_count} | "
                            f"preprocess: {avg_preprocess:.1f}ms | "
                            f"tensor_creation: {avg_tensor:.1f}ms | "
                            f"model_inference: {avg_model:.1f}ms | "
                            f"cpu_transfer: {avg_cpu:.1f}ms | "
                            f"publish: {avg_publish:.1f}ms | "
                            f"total: {avg_inference:.1f}ms"
                        )
                
                # Send feedback
                remaining_time = max(0.0, duration - elapsed_time)
                feedback_msg = ExecuteBehavior.Feedback()
                feedback_msg.elapsed_time = float(elapsed_time)
                feedback_msg.remaining_time = float(remaining_time)
                feedback_msg.status = f"Executing {behavior_name}"
                goal_handle.publish_feedback(feedback_msg)
                
                # Maintain loop rate
                sleep_time = period - (time.time() - loop_start)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            
            # Log final timing summary
            if len(loop_times) > 0:
                overall_avg_loop = np.mean(loop_times) * 1000
                overall_max_loop = np.max(loop_times) * 1000
                overall_min_loop = np.min(loop_times) * 1000
                overall_std_loop = np.std(loop_times) * 1000
                overall_avg_inference = np.mean(inference_times) * 1000
                overall_max_inference = np.max(inference_times) * 1000
                overall_actual_hz = 1000.0 / overall_avg_loop if overall_avg_loop > 0 else 0
                
                # Calculate jitter as percentage of target period
                jitter_percent = (overall_std_loop / (period * 1000)) * 100
                
                self.get_logger().info(
                    f"[TIMING SUMMARY] total_iterations={iteration_count} | "
                    f"loop: avg={overall_avg_loop:.1f}ms, min={overall_min_loop:.1f}ms, max={overall_max_loop:.1f}ms | "
                    f"jitter(std)={overall_std_loop:.2f}ms ({jitter_percent:.1f}% of target period) | "
                    f"inference: avg={overall_avg_inference:.1f}ms, max={overall_max_inference:.1f}ms | "
                    f"actual_hz={overall_actual_hz:.1f} (target={inference_hz})"
                )
                
                # Log final detailed timing breakdown
                if len(preprocess_times) > 0:
                    overall_avg_preprocess = np.mean(preprocess_times) * 1000
                    overall_avg_tensor = np.mean(tensor_creation_times) * 1000
                    overall_avg_model = np.mean(model_inference_times) * 1000
                    overall_avg_cpu = np.mean(cpu_transfer_times) * 1000
                    overall_avg_publish = np.mean(publish_times) * 1000
                    
                    self.get_logger().info(
                        f"[DETAILED TIMING SUMMARY] total_iterations={iteration_count} | "
                        f"preprocess: avg={overall_avg_preprocess:.1f}ms | "
                        f"tensor_creation: avg={overall_avg_tensor:.1f}ms | "
                        f"model_inference: avg={overall_avg_model:.1f}ms | "
                        f"cpu_transfer: avg={overall_avg_cpu:.1f}ms | "
                        f"publish: avg={overall_avg_publish:.1f}ms | "
                        f"total_inference: avg={overall_avg_inference:.1f}ms"
                    )
            
            # Stop robot and move to end pose
            self._stop_robot()
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose} (time: {end_pose_time}s)")
                if not self.call_arm_goto_service(end_pose, end_pose_time):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE", "Failed to move arm to end pose"
            
            if early_termination:
                self.get_logger().info(f"Learned behavior {behavior_name} completed early due to progress threshold")
                return "SUCCESS", "Completed early due to progress threshold being reached"
            else:
                self.get_logger().info(f"Learned behavior {behavior_name} completed successfully")
                return "SUCCESS", "Completed full duration successfully"
            
        except Exception as e:
            self.get_logger().error(f"Error in learned behavior execution: {e}")
            self._stop_robot()
            return "FAILURE", f"Exception during execution: {str(e)}"
    
    def _execute_poses_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a poses-based behavior."""
        self.get_logger().info(f"Executing poses behavior: {behavior_name}")
        
        poses = behavior_config['execution'].get('poses', [])
        steps = int(behavior_config['execution'].get('steps', len(poses)))
        
        try:
            for i, pose in enumerate(poses):
                # Check for cancellation
                if self._cancel_requested.is_set():
                    return "CANCELLED", "User requested cancellation"
                
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
                    return "FAILURE", f"Failed to reach pose {i+1}/{len(poses)}"
                
                time.sleep(steps)
            
            self.get_logger().info(f"Poses behavior {behavior_name} completed successfully")
            return "SUCCESS", f"All {len(poses)} poses executed successfully"
            
        except Exception as e:
            self.get_logger().error(f"Error in poses behavior execution: {e}")
            return "FAILURE", f"Exception during poses execution: {str(e)}"
    
    def _execute_replay_behavior(self, goal_handle, behavior_name, behavior_config):
        """Execute a replay-based behavior from H5 file."""
        self.get_logger().info(f"Executing replay behavior: {behavior_name}")
        
        # Load from primitives/{behavior_name}/ directory
        primitive_dir = os.path.join(self.primitives_directory, behavior_name)
        replay_file = behavior_config['execution'].get('replay_file')
        file_path = os.path.join(primitive_dir, replay_file)
        start_pose = behavior_config['execution'].get('start_pose')
        end_pose = behavior_config['execution'].get('end_pose')
        start_pose_time = behavior_config['execution'].get('start_pose_time', 1)
        end_pose_time = behavior_config['execution'].get('end_pose_time', 1)
        
        if not file_path or not os.path.exists(file_path):
            self.get_logger().error(f"Replay file not found: {file_path}")
            return "FAILURE", f"Replay file not found: {file_path}"
        
        try:
            # Get replay frequency from config (default to 12.0 Hz)
            replay_hz = behavior_config['execution'].get('replay_frequency', 12.0)
            
            # Load H5 file and extract actions
            with h5py.File(file_path, 'r') as h5file:
                if 'action' not in h5file:
                    self.get_logger().error("No 'action' dataset found in H5 file")
                    return "FAILURE", "No 'action' dataset found in H5 file"
                
                actions = h5file['action'][:]  # Shape: (n_steps, action_dim)
                self.get_logger().info(f"Loaded {actions.shape[0]} action steps from {file_path}")
            
            if actions.shape[0] == 0:
                self.get_logger().error("No actions found in replay file")
                return "FAILURE", "No actions found in replay file"
            
            # Move to start pose if specified, otherwise use first action from H5
            if start_pose:
                # Use start_pose from metadata
                self.get_logger().info(f"Moving to start pose from metadata: {start_pose} (time: {start_pose_time}s)")
                if not self.call_arm_goto_service(start_pose, start_pose_time):
                    self.get_logger().error("Failed to move to start pose")
                    return "FAILURE", "Failed to move to start pose"
            else:
                # Fall back to first action from H5 file
                # Action format: [arm_joints(6), linear.x, angular.z, progress?, termination?]
                first_action = actions[0]
                first_arm_position = first_action[0:6].tolist()  # First 6 elements are arm joint positions
                self.get_logger().info(f"Moving to initial arm position from H5: {first_arm_position} (time: {start_pose_time}s)")
                if not self.call_arm_goto_service(first_arm_position, start_pose_time):
                    self.get_logger().error("Failed to move to initial arm position")
                    return "FAILURE", "Failed to move to initial arm position"
            
            
            # Replay parameters
            total_steps = actions.shape[0]
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
                        self.call_arm_goto_service(end_pose, end_pose_time)
                    return "CANCELLED", "User requested cancellation"
                
                # Get current action
                action = actions[step_idx]
                
                # Extract arm commands (first 6 elements = joint positions)
                arm_msg = Float64MultiArray()
                arm_msg.data = [float(v) for v in action[0:6]]
                self.arm_state_pub.publish(arm_msg)
                
                # Extract cmd_vel commands (elements 6-7 = linear.x, angular.z)
                twist_msg = Twist()
                twist_msg.linear.x = float(action[6]) / 2.0
                twist_msg.angular.z = float(action[7]) / 2.0
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
            
            # Stop robot after replay and hold last arm position
            self._stop_robot()
            
            # Hold the last arm position to prevent drift
            last_arm_position = actions[-1][0:6].tolist()
            arm_msg = Float64MultiArray()
            arm_msg.data = [float(v) for v in last_arm_position]
            self.arm_state_pub.publish(arm_msg)
            self.get_logger().info(f"Holding last arm position: {last_arm_position}")
            
            # Move to end pose if specified
            if end_pose:
                self.get_logger().info(f"Moving to end pose: {end_pose} (time: {end_pose_time}s)")
                if not self.call_arm_goto_service(end_pose, end_pose_time):
                    self.get_logger().error("Failed to move to end pose")
                    return "FAILURE", "Failed to move to end pose"
            
            self.get_logger().info(f"Replay behavior {behavior_name} completed successfully")
            return "SUCCESS", f"Replay completed successfully with {total_steps} steps"
            
        except Exception as e:
            self.get_logger().error(f"Error in replay behavior execution: {e}")
            self._stop_robot()
            return "FAILURE", f"Exception during replay execution: {str(e)}"
    
    def _load_policy_for_behavior(self, checkpoint_path, action_dim=8):
        """Load ACT policy for a specific behavior."""
        try:
            load_start = time.time()
            
            # Clean up previous policy
            if self.current_policy:
                del self.current_policy
                torch.cuda.empty_cache() if torch.cuda.is_available() else None
            
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
            
            # Create and load policy with specified action dimension
            self.get_logger().info(f"Creating ACTPolicy with action_dim={action_dim} on device={self.device}")
            policy_config = create_act_config(action_dim=action_dim)
            self.current_policy = ACTPolicy(config=policy_config, dataset_stats=dataset_stats).to(self.device)
            
            self.get_logger().info(f"Loading checkpoint: {checkpoint_path}")
            state_dict = torch.load(checkpoint_path, map_location=self.device)
            
            # Handle different checkpoint formats (direct state_dict or wrapped in dict)
            if isinstance(state_dict, dict) and 'model_state_dict' in state_dict:
                state_dict = state_dict['model_state_dict']
                self.get_logger().info("Extracted state_dict from checkpoint dict")
            elif isinstance(state_dict, dict) and 'state_dict' in state_dict:
                state_dict = state_dict['state_dict']
                self.get_logger().info("Extracted state_dict from checkpoint dict")
            
            # Strip prefixes from keys if present (from torch.compile() and/or DDP)
            # Check for _orig_mod. prefix (from torch.compile())
            has_orig_mod = any(key.startswith('_orig_mod.') for key in state_dict.keys())
            # Check for module. prefix (from DDP, though train_dist.py uses policy.module so shouldn't have this)
            has_module = any(key.startswith('module.') for key in state_dict.keys())
            
            if has_orig_mod or has_module:
                self.get_logger().info(f"Stripping prefixes from checkpoint keys (orig_mod: {has_orig_mod}, module: {has_module})")
                cleaned_state_dict = {}
                for key, value in state_dict.items():
                    # Remove both prefixes if present (handle nested cases like module._orig_mod.)
                    cleaned_key = key
                    if cleaned_key.startswith('module.'):
                        cleaned_key = cleaned_key.replace('module.', '', 1)
                    if cleaned_key.startswith('_orig_mod.'):
                        cleaned_key = cleaned_key.replace('_orig_mod.', '', 1)
                    cleaned_state_dict[cleaned_key] = value
                state_dict = cleaned_state_dict
            
            # Load state dict with strict=False to handle potential mismatches gracefully
            load_result = self.current_policy.load_state_dict(state_dict, strict=False)
            
            # Log any missing or unexpected keys for debugging
            if load_result.missing_keys:
                self.get_logger().warn(f"Missing keys in checkpoint: {load_result.missing_keys[:5]}..." 
                                     if len(load_result.missing_keys) > 5 else f"Missing keys: {load_result.missing_keys}")
            if load_result.unexpected_keys:
                self.get_logger().warn(f"Unexpected keys in checkpoint: {load_result.unexpected_keys[:5]}..." 
                                     if len(load_result.unexpected_keys) > 5 else f"Unexpected keys: {load_result.unexpected_keys}")
            
            if not load_result.missing_keys and not load_result.unexpected_keys:
                self.get_logger().info("Checkpoint loaded successfully with all keys matching")
            self.current_policy.eval()
            
            # Reset policy to clear any cached states
            self.current_policy.reset()
            
            # Apply torch.compile() for optimized inference (matching training setup)
            try:
                self.get_logger().info("Compiling model with torch.compile() for optimized inference...")
                self.current_policy = torch.compile(self.current_policy, mode='default')
                self.get_logger().info("Model compilation setup completed")
                self.get_logger().info("Note: First inference will trigger actual compilation")
                # Verify compilation was applied
                if hasattr(self.current_policy, '_orig_mod'):
                    self.get_logger().info("✓ Compilation verified: model has _orig_mod attribute")
                else:
                    self.get_logger().warn("⚠ Compilation may not have been applied (no _orig_mod attribute)")
            except Exception as e:
                self.get_logger().warn(f"Model compilation failed: {e}, falling back to uncompiled model")
            
            # Store the current action dimension
            self.current_action_dim = action_dim
            
            load_time = time.time() - load_start
            self.get_logger().info(f"Policy loaded in {load_time:.2f}s from {checkpoint_path} with action_dim={action_dim}")
            
            # Log model info
            param_count = sum(p.numel() for p in self.current_policy.parameters())
            self.get_logger().info(f"Policy parameters: {param_count:,} ({param_count/1e6:.1f}M)")
            
            # Warm-up inference (run multiple times to trigger compilation)
            self.get_logger().info("Running warm-up inference (triggering compilation)...")
            warmup_start = time.time()
            dummy_batch = {
                "observation.image_camera_1": torch.zeros(1, 3, 224, 224, device=self.device),
                "observation.image_camera_2": torch.zeros(1, 3, 224, 224, device=self.device),
                "observation.state": torch.zeros(1, 6, device=self.device)
            }
            # Run multiple warmup iterations to ensure compilation completes
            num_warmup = 5
            for i in range(num_warmup):
                with torch.no_grad():
                    _ = self.current_policy.select_action(dummy_batch)
                if self.device.type == 'cuda':
                    torch.cuda.synchronize()
            warmup_time = time.time() - warmup_start
            self.get_logger().info(f"Warm-up inference completed ({num_warmup} iterations) in {warmup_time:.2f}s (avg: {warmup_time/num_warmup*1000:.1f}ms per inference)")
            
            return True
            
        except Exception as e:
            self.get_logger().error(f"Failed to load policy: {e}")
            return False
    
    # ... existing sensor callbacks and utility methods from policy.py ...
    def image1_callback(self, msg: Image):
        try:
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image1_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        try:
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
            self.latest_image2_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        self.latest_joint_state = msg
        self.latest_joint_timestamp = rclpy.time.Time.from_msg(msg.header.stamp)

    def _run_inference_once(self, detailed_timing=None):
        """Run one inference step with current policy.
        
        Args:
            detailed_timing: Optional dict to store detailed timing breakdowns
        """
        if not self.current_policy or self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            return None

        try:
            # Image preprocessing timing
            preprocess_start = time.time()
            # Keep images in BGR format to match training pipeline (recorder → H5 → WebDataset → model all use BGR)
            # Use INTER_AREA to match training preprocessing (webdataset.py uses INTER_AREA)
            img1 = cv2.resize(self.latest_image1, self.image_size, interpolation=cv2.INTER_AREA)
            img2 = cv2.resize(self.latest_image2, self.image_size, interpolation=cv2.INTER_AREA)
            img1 = img1.astype(np.float32) / 255.0
            img2 = img2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
            preprocess_time = time.time() - preprocess_start
            
            # Tensor creation and CPU->GPU transfer timing
            tensor_start = time.time()
            img1_tensor = torch.tensor(img1, device=self.device).unsqueeze(0)
            img2_tensor = torch.tensor(img2, device=self.device).unsqueeze(0)

            # Joint state preprocessing
            qpos = np.asarray(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos, device=self.device).unsqueeze(0)
            
            if self.device.type == 'cuda':
                torch.cuda.synchronize()  # Wait for GPU transfers to complete
            tensor_creation_time = time.time() - tensor_start

            batch = {
                "observation.image_camera_1": img1_tensor,
                "observation.image_camera_2": img2_tensor,
                "observation.state": qpos_tensor
            }

            # Policy forward pass timing
            inference_start = time.time()
            if self.device.type == 'cuda':
                torch.cuda.synchronize()  # Ensure previous operations are done
            with torch.no_grad():
                action = self.current_policy.select_action(batch)
                if self.device.type == 'cuda':
                    torch.cuda.synchronize()  # Wait for inference to complete
            inference_time = time.time() - inference_start
            
            # GPU->CPU transfer timing
            cpu_transfer_start = time.time()
            action_np = action.cpu().numpy().squeeze(0)
            cpu_transfer_time = time.time() - cpu_transfer_start

            # Check action dimensions match expected
            if action_np.shape[0] < self.current_action_dim:
                self.get_logger().error(f"Action has wrong dimensions. Expected {self.current_action_dim}, got {action_np.shape[0]}")
                return None

            progress = None
            
            # Extract progress value for 10-dimensional actions
            if self.current_action_dim >= 10:
                progress = float(action_np[8])  # 9th element (index 8) - progress

            # Publish commands timing
            publish_start = time.time()
            # Publish commands (using first 8 dimensions)
            twist_msg = Twist()
            twist_msg.linear.x = float(action_np[6]) / 2.0  # 7th element
            twist_msg.angular.z = float(action_np[7]) / 2.0  # 8th element
            self.cmd_vel_pub.publish(twist_msg)

            arm_msg = Float64MultiArray()
            arm_msg.data = [float(v) for v in action_np[:6]]  # First 6 elements
            self.arm_state_pub.publish(arm_msg)
            publish_time = time.time() - publish_start
            
            # Store detailed timing if requested
            if detailed_timing is not None:
                detailed_timing['preprocess'] = preprocess_time
                detailed_timing['tensor_creation'] = tensor_creation_time
                detailed_timing['model_inference'] = inference_time
                detailed_timing['cpu_transfer'] = cpu_transfer_time
                detailed_timing['publish'] = publish_time
                detailed_timing['total'] = preprocess_time + tensor_creation_time + inference_time + cpu_transfer_time + publish_time
            
            # Return progress value for early termination check
            return progress

        except Exception as e:
            self.get_logger().error(f"Error during inference: {e}")
            return None

    def _stop_robot(self):
        """Stop the robot by sending zero commands."""
        twist_msg = Twist()
        twist_msg.linear.x = 0.0
        twist_msg.angular.z = 0.0
        self.cmd_vel_pub.publish(twist_msg)
    
    def _set_head_ai_position(self):
        """Set the head to AI position for recording/policy execution."""
        try:
            if not self.head_ai_position_client.service_is_ready():
                self.get_logger().warn("Head AI position service not available")
                return
            
            self.head_ai_position_client.call_async(Trigger.Request())
            self.get_logger().info("Head AI position command sent")
            time.sleep(3.0)  # Wait for head to move to AI position
        except Exception as e:
            self.get_logger().error(f"Error setting AI position: {e}")
    
    def _check_sensor_availability(self):
        """Check if all required sensors are providing data."""
        current_time = self.get_clock().now()
        timeout_threshold = 2.0  # seconds
        
        # Check if we have received any data at all
        if self.latest_image1 is None:
            self.get_logger().warn("Camera 1 (/mars/main_camera/left/image_raw) has never received data")
            return False
        
        if self.latest_image2 is None:
            self.get_logger().warn("Camera 2 (/mars/arm/image_raw) has never received data")
            return False
        
        if self.latest_joint_state is None:
            self.get_logger().warn("Joint state (/mars/arm/state) has never received data")
            return False
        
        # Check if data is recent (within timeout threshold)
        if self.latest_image1_timestamp is not None:
            time_diff = (current_time - self.latest_image1_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Camera 1 data is stale ({time_diff:.2f}s old)")
                return False
        
        if self.latest_image2_timestamp is not None:
            time_diff = (current_time - self.latest_image2_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Camera 2 data is stale ({time_diff:.2f}s old)")
                return False
        
        if self.latest_joint_timestamp is not None:
            time_diff = (current_time - self.latest_joint_timestamp).nanoseconds / 1e9
            if time_diff > timeout_threshold:
                self.get_logger().warn(f"Joint state data is stale ({time_diff:.2f}s old)")
                return False
        
        return True

    def call_arm_goto_service(self, position, time_duration=5):
        """Call the arm goto service with specified position and wait for completion."""
        if not self.arm_goto_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().error("Arm goto service not available")
            return False
        
        # Ensure position is a list of floats (JSON parsing may produce other types)
        if not position or len(position) == 0:
            self.get_logger().warn("Empty position provided to arm goto service, skipping")
            return True
            
        request = GotoJS.Request()
        request.data.data = [float(p) for p in position]
        request.time = float(time_duration)
        
        try:
            future = self.arm_goto_client.call_async(request)
            self.get_logger().info(f"Arm goto service called with position: {[float(p) for p in position]} (time: {time_duration}s)")
            
            # Wait for the service call to complete (service blocks for time_duration internally)
            timeout_sec = time_duration + 0.2  # Small buffer for network/processing overhead
            start_wait = time.time()
            while not future.done():
                if time.time() - start_wait > timeout_sec:
                    self.get_logger().error(f"Arm goto service timed out after {timeout_sec}s")
                    return False
                time.sleep(0.05)  # Small sleep to avoid busy-waiting
            
            # Check result
            result = future.result()
            if result is not None:
                self.get_logger().info(f"Arm goto service completed successfully")
                return True
            else:
                self.get_logger().error("Arm goto service returned None result")
                return False
                
        except Exception as e:
            self.get_logger().error(f"Error calling arm goto service: {e}, position type: {type(position)}, position: {position}")
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
