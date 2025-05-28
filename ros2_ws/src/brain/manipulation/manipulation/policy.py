#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray  # For arm commands
from std_srvs.srv import Trigger  # Simple service
from maurice_msgs.srv import GotoJS  # Add this import for arm service
from cv_bridge import CvBridge
import numpy as np
import torch
import pickle
import os
import cv2
from geometry_msgs.msg import Twist
import json
import torch.amp
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

# Enable CUDNN for better performance
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

# Import your policy class and trajectory generator.
from act_test.ACT import ACTPolicy, ACTConfig

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
        n_action_steps=15,  # CHANGED: Match training config for efficiency
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
        self.get_logger().info("Inference node started.")
        self.bridge = CvBridge()
        self.image_size = (640, 480)

        # Set device and load the policy model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        # Load normalization stats first
        checkpoint_path ='/home/jetson1/maurice-prod/ros2_ws/src/brain/manipulation/ckpts/PaperCorner_Filtered_20250526_213031/act_policy_epoch_90000.pth'
        checkpoint_path = os.path.expanduser(checkpoint_path)
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

        # Inference control variables
        self.inference_running = False
        self.inference_start_time = None
        self.inference_duration = 20.0  # 20 seconds
        
        # Arm positions for start and end
        self.start_position = [0.143, -0.434, 0.147, 0.788, 0.060, 0.701]
        self.end_position = [0.853, -0.457, 1.295, -0.933, -0.049, 0.0]
        
        # Subscribers for images and joint state with sensor QoS
        self.create_subscription(Image, '/color/image', self.image1_callback, image_qos)
        self.create_subscription(Image, '/image_raw', self.image2_callback, image_qos)
        self.create_subscription(JointState, '/maurice_arm/state', self.joint_state_callback, 10)

        # Publishers for twist and arm commands
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)

        # Service client for arm positioning
        self.arm_goto_client = self.create_client(GotoJS, '/maurice_arm/goto_js')
        
        # Service server for starting inference
        self.run_service = self.create_service(Trigger, '/policy/run', self.run_policy_callback)
        
        # Timer to run the inference loop at 15 Hz (but only when active)
        self.timer = self.create_timer(1/15.0, self.inference_loop)
        
        self.get_logger().info("Policy loaded and ready. Call '/policy/run' service to start inference.")

    def run_policy_callback(self, request, response):
        """Service callback to start/restart policy inference for fixed duration."""
        if self.inference_running:
            response.success = False
            response.message = "Policy is already running"
            self.get_logger().warn("Policy run requested but already running")
        else:
            # First, move arm to start position
            self.get_logger().info("Moving arm to start position...")
            if self.call_arm_goto_service(self.start_position, 5):
                # Wait for arm movement to complete
                time.sleep(5.0)
                
                # Now start inference
                self.inference_running = True
                self.inference_start_time = time.time()
                response.success = True
                response.message = f"Arm moved to start position. Policy started for {self.inference_duration} seconds"
                self.get_logger().info(f"Policy inference started for {self.inference_duration} seconds")
            else:
                response.success = False
                response.message = "Failed to move arm to start position"
                self.get_logger().error("Failed to move arm to start position")
        
        return response

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
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        try:
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='passthrough')
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        self.latest_joint_state = msg

    ####################################################
    # Simplified Inference Loop
    ####################################################
    def inference_loop(self):
        """Simplified inference that runs for fixed duration when triggered."""
        if not self.inference_running:
            return
            
        # Check if inference duration has expired
        if time.time() - self.inference_start_time > self.inference_duration:
            self.inference_running = False
            self.get_logger().info("Policy inference stopped after timeout")
            
            # Send zero commands to stop the robot
            twist_msg = Twist()
            twist_msg.linear.x = 0.0
            twist_msg.angular.z = 0.0
            self.cmd_vel_pub.publish(twist_msg)
            
            # Move arm to end position
            self.get_logger().info("Moving arm to end position...")
            self.call_arm_goto_service(self.end_position, 5)
            
            return

        if self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            return

        start_time = time.time()

        # Preprocess images
        try:
            img1 = cv2.resize(self.latest_image1, self.image_size)
            img2 = cv2.resize(self.latest_image2, self.image_size)
            img1 = img1.astype(np.float32) / 255.0
            img2 = img2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
            img1_tensor = torch.tensor(img1).to(self.device).unsqueeze(0)
            img2_tensor = torch.tensor(img2).to(self.device).unsqueeze(0)
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
            return

        # Process the joint state
        try:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos).unsqueeze(0).to(self.device)
        except Exception as e:
            self.get_logger().error(f"Error processing joint state: {e}")
            return

        # Create batch dictionary for the new API
        batch = {
            "observation.image_camera_1": img1_tensor,
            "observation.image_camera_2": img2_tensor,
            "observation.state": qpos_tensor
        }

        # Get next action from policy (handles all buffering/ensembling internally)
        with torch.no_grad():
            try:
                action = self.policy.select_action(batch)  # Returns a single action tensor
                action_np = action.cpu().numpy()
                
                # Squeeze batch dimension if present
                if action_np.ndim > 1:
                    action_np = action_np.squeeze(0)  # Remove batch dimension
                
                # Check if action has the expected dimensions
                if len(action_np) < 8:
                    self.get_logger().error(f"Action has wrong dimensions. Expected 8, got {len(action_np)}")
                    return
                
                # Extract and publish the twist command (last two elements)
                twist_msg = Twist()
                twist_msg.linear.x = float(action_np[-2]) / 2
                twist_msg.angular.z = float(action_np[-1]) / 2
                self.cmd_vel_pub.publish(twist_msg)

                # Extract and publish the arm command (first six elements)
                arm_msg = Float64MultiArray()
                arm_msg.data = [float(x) for x in action_np[:6]]
                self.arm_state_pub.publish(arm_msg)

                self.get_logger().info(f"Inference cycle time: {time.time() - start_time:.3f} seconds")
                
            except Exception as e:
                self.get_logger().error(f"Error during inference: {e}")

def main(args=None):
    rclpy.init(args=args)
    node = InferenceNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Inference node shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
