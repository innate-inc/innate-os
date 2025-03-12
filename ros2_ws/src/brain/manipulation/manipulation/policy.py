#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray  # For arm command
from cv_bridge import CvBridge
import numpy as np
import torch
import pickle
import os
import cv2
from geometry_msgs.msg import Twist
import json

# Import your policy class and any necessary constants/configs.
from InnateACT.policy import ACTPolicy
from trajectory import cubic_trajectory

# Define the policy configuration (ensure these match your training configuration)
policy_config = {
    'lr': 5e-5,
    'weight_decay': 1e-4,
    'num_queries': 100,
    'kl_weight': 100,
    'hidden_dim': 512,
    'dim_feedforward': 3200,
    'lr_backbone': 5e-5,
    'backbone': 'resnet18',
    'enc_layers': 4,
    'dec_layers': 7,
    'nheads': 8,
    'camera_names': ['camera_1', 'camera_2'],
    'position_embedding': "sine",
    'masks': False,
    'dilation': False,
    'dropout': 0.1,
    'pre_norm': False,
    'state_dim': 6,
    'action_dim': 8  # Adjust if needed
}

class InferenceNode(Node):
    def __init__(self):
        super().__init__('inference_node')
        self.get_logger().info("Inference node started (using passthrough for images).")
        self.bridge = CvBridge()

        # Image size definition
        self.image_size = (640, 480)

        # Set device and load the policy model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.policy = ACTPolicy(policy_config).to(self.device)
        checkpoint_path = '/home/jetson1/maurice-prod/ros2_ws/src/brain/manipulation/ckpts/Paper_4_20250312_0345/policy_epoch_20000_seed_100.ckpt'
        checkpoint_path = os.path.expanduser(checkpoint_path)
        # Load normalization stats and metadata from the same directory as checkpoint
        checkpoint_dir = os.path.dirname(checkpoint_path)
        stats_path = os.path.join(checkpoint_dir, 'dataset_stats.pkl')
        metadata_path = os.path.join(checkpoint_dir, 'metadata.json')
        
        try:
            with open(stats_path, 'rb') as f:
                self.norm_stats = pickle.load(f)
            self.get_logger().info("Normalization stats loaded.")
        except Exception as e:
            self.get_logger().error(f"Failed to load normalization stats: {e}")
            self.norm_stats = None

        try:
            with open(metadata_path, 'r') as f:
                self.metadata = json.load(f)
            self.get_logger().info("Metadata loaded successfully.")
        except Exception as e:
            self.get_logger().error(f"Failed to load metadata: {e}")
            self.metadata = None

        try:
            state_dict = torch.load(checkpoint_path, map_location=self.device)
            self.policy.load_state_dict(state_dict)
            self.policy.eval()
            self.get_logger().info("Policy loaded successfully.")
        except Exception as e:
            self.get_logger().error(f"Failed to load policy checkpoint: {e}")
        
        # Variables to hold the latest sensor data
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None

        # Subscribers for the two image topics and joint state topic
        self.create_subscription(Image, '/color/image', self.image1_callback, 10)
        self.create_subscription(Image, '/image_raw', self.image2_callback, 10)
        self.create_subscription(JointState, '/maurice_arm/state', self.joint_state_callback, 10)

        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        # Publishing arm commands on a topic '/maurice_arm/commands'
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)



        # Timer to run the publishing loop at 30 Hz (every ~0.033 seconds)
        self.timer = self.create_timer(1/15.0, self.inference_loop)

        # Create publishers for cmd_vel and arm state command
        

        # Buffer to hold the predicted actions (each action is an 8-element vector)
        self.action_buffer = []
        self.first_step=True

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

    def run_inference(self):
        """Run the policy network to predict 100 time steps, then return the first 20 actions."""
        
        # Make sure all sensor data are available
        if self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            self.get_logger().info("Waiting for all topics to be received...")
            return None
            
        if self.first_step:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            if self.metadata and 'average_first_step_action' in self.metadata:
                start_pos = np.array(self.metadata['average_first_step_action'])[:6]
                # Generate a smooth trajectory from current position to start position
                _, base_trajectory = cubic_trajectory(qpos, start_pos, total_time=1.0, freq=15)
                print(base_trajectory.shape)
                # Pad each timestep with two zeros for base motion
                padded_trajectory = np.pad(base_trajectory, ((0, 0), (0, 2)), mode='constant', constant_values=0)
                
                self.first_step = False
                return padded_trajectory.tolist()  # Convert numpy array to list
            else:
                self.first_step = False  # Continue without trajectory if no metadata
                return None

        try:
            # Preprocess images
            img1 = cv2.resize(self.latest_image1, self.image_size)
            img2 = cv2.resize(self.latest_image2, self.image_size)
            img1 = img1.astype(np.float32) / 255.0
            img2 = img2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
            img1_tensor = torch.tensor(img1).to(self.device)
            img2_tensor = torch.tensor(img2).to(self.device)
            images = torch.stack([img1_tensor, img2_tensor], dim=0).unsqueeze(0)
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
            return None

        # Process the joint state
        try:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos).unsqueeze(0).to(self.device)
            if self.norm_stats is not None:
                qpos_mean = torch.tensor(self.norm_stats["qpos_mean"], dtype=qpos_tensor.dtype, device=self.device)
                qpos_std = torch.tensor(self.norm_stats["qpos_std"], dtype=qpos_tensor.dtype, device=self.device)
                qpos_tensor = (qpos_tensor - qpos_mean) / qpos_std
        except Exception as e:
            self.get_logger().error(f"Error processing joint state: {e}")
            return None

        # Run the policy to predict the future actions
        with torch.no_grad():
            output = self.policy(qpos_tensor, images)
            if self.norm_stats is not None and "action_mean" in self.norm_stats:
                action_mean = torch.tensor(self.norm_stats["action_mean"], dtype=output.dtype, device=self.device)
                action_std = torch.tensor(self.norm_stats["action_std"], dtype=output.dtype, device=self.device)
                unnormalized_actions = output * action_std + action_mean
                # unnormalized_actions is assumed to be [batch, 100, 8]
                # Take the first 20 predicted actions and convert to list
                actions = unnormalized_actions[0, :20, :].cpu().numpy().tolist()  # Convert to CPU, then numpy, then list
                return actions
            else:
                return None

    def inference_loop(self):
        
        start_time = time.time()
        # If the buffer is empty, run inference to get a new sequence.
        if not self.action_buffer:
            actions = self.run_inference()
            if actions is None:
                return
            # Convert the tensor to a list of actions (each is an 8-element vector)
            self.action_buffer = actions
            self.get_logger().info("New action buffer computed with 20 actions.")

        # Pop the next action from the buffer and publish it.
        next_action = self.action_buffer.pop(0)

        # The twist command is taken from the last two elements.
        twist_msg = Twist()
        twist_msg.linear.x = next_action[-2]
        twist_msg.angular.z = next_action[-1]
        self.cmd_vel_pub.publish(twist_msg)
        self.get_logger().info(f"Published Twist: linear.x={twist_msg.linear.x}, angular.z={twist_msg.angular.z}")

        # The arm command is taken from the first six elements.
        arm_msg = Float64MultiArray()
        arm_msg.data = next_action[:6]
        self.arm_state_pub.publish(arm_msg)
        self.get_logger().info(f"Published Arm Command: {arm_msg.data}")

        print(f"Cycle time: {time.time() - start_time:.3f} seconds")

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
