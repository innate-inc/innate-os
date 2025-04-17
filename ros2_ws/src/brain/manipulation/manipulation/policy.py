#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from std_msgs.msg import Float64MultiArray  # For arm commands
from cv_bridge import CvBridge
import numpy as np
import torch
import pickle
import os
import cv2
from geometry_msgs.msg import Twist
import json
import torch.amp

# Import your policy class and trajectory generator.
from InnateACT.policy import ACTPolicy

# Import the TemporalEnsembler from a separate file.
from manipulation.ensemble import ACTTemporalEnsembler

# Import the new service type.
from maurice_msgs.srv import GotoJS

# Define the policy configuration (ensure these match your training configuration)
policy_config = {
    'lr': 5e-5,
    'weight_decay': 1e-4,
    'num_queries': 30,
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
    'action_dim': 8
}

####################################################
# Temporal Ensembling Configuration
####################################################
# Global variables for temporal ensembling
USE_TEMPORAL_ENSEMBLING = False
TEMPORAL_ENSEMBLE_COEFF = 0.3
CHUNK_SIZE = 10

class InferenceNode(Node):
    def __init__(self):
        super().__init__('inference_node')
        self.get_logger().info("Inference node started.")
        self.bridge = CvBridge()
        self.image_size = (640, 480)

        # Set device and load the policy model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.policy = ACTPolicy(policy_config).to(self.device).half()  # Convert model to half precision
        checkpoint_path = '~/maurice-prod/ros2_ws/src/brain/manipulation/ckpts/Tape_20250331_1848/policy_epoch_24000_seed_100.ckpt'
        checkpoint_path = os.path.expanduser(checkpoint_path)
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
            
            # Compiled model will be initialized on first inference
            self.compiled_policy = None
            self.get_logger().info("Model will be compiled with float16 support on first inference.")
        except Exception as e:
            self.get_logger().error(f"Failed to load policy checkpoint: {e}")

        # Variables to hold the latest sensor data
        self.latest_image1 = None
        self.latest_image2 = None
        self.latest_joint_state = None

        # Subscribers for images and joint state
        self.create_subscription(Image, '/color/image', self.image1_callback, 10)
        self.create_subscription(Image, '/image_raw', self.image2_callback, 10)
        self.create_subscription(JointState, '/maurice_arm/state', self.joint_state_callback, 10)

        # Publishers for twist and arm commands
        self.cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.arm_state_pub = self.create_publisher(Float64MultiArray, '/maurice_arm/commands', 10)

        # Timer to run the inference loop at 10 Hz
        self.timer = self.create_timer(1/30.0, self.inference_loop)

        # Action buffer for storing predicted actions
        self.action_buffer = []

        # If using temporal ensembling, create an ensembler instance
        if USE_TEMPORAL_ENSEMBLING:
            self.temporal_ensembler = ACTTemporalEnsembler(TEMPORAL_ENSEMBLE_COEFF, CHUNK_SIZE)
            self.get_logger().info("Temporal ensembling enabled.")
        else:
            self.temporal_ensembler = None

        # Call the /maurice_arm/goto_js service at startup
        self.call_goto_js_service()

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
    # Service Call for Initial Joint State
    ####################################################
    def call_goto_js_service(self):
        self.goto_client = self.create_client(GotoJS, '/maurice_arm/goto_js')
        while not self.goto_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info("Waiting for /maurice_arm/goto_js service to become available...")
        req = GotoJS.Request()
        # Use metadata if available to set the initial joint state; otherwise, use zeros.
        if self.metadata and 'average_first_step_action' in self.metadata:
            joint_data = self.metadata['average_first_step_action'][:6]
            req.data = Float64MultiArray(data=joint_data)
            req.time = 1  # for example, 1 second to reach the target state
        else:
            self.get_logger().warn("Metadata not available for initial joint state. Using default zeros.")
            req.data = Float64MultiArray(data=[0.0] * 6)
            req.time = 1
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
    # Policy Inference
    ####################################################
    def run_inference(self):
        """Run the policy network to predict actions."""
        if self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            # self.get_logger().info("Waiting for all topics to be received...")
            return None

        # Preprocess images
        try:
            img1 = cv2.resize(self.latest_image1, self.image_size)
            img2 = cv2.resize(self.latest_image2, self.image_size)
            img1 = img1.astype(np.float32) / 255.0
            img2 = img2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
            img1_tensor = torch.tensor(img1).to(self.device).half()  # Convert to half precision
            img2_tensor = torch.tensor(img2).to(self.device).half()  # Convert to half precision
            images = torch.stack([img1_tensor, img2_tensor], dim=0).unsqueeze(0)
            # self.get_logger().info(f"images.shape: {images.shape}")
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
            return None

        # Process the joint state
        try:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos).unsqueeze(0).to(self.device).half()  # Convert to half precision
            if self.norm_stats is not None:
                qpos_mean = torch.tensor(self.norm_stats["qpos_mean"], dtype=torch.float16, device=self.device)
                qpos_std = torch.tensor(self.norm_stats["qpos_std"], dtype=torch.float16, device=self.device)
                qpos_tensor = (qpos_tensor - qpos_mean) / qpos_std
        except Exception as e:
            self.get_logger().error(f"Error processing joint state: {e}")
            return None

        # Run the policy network
        with torch.no_grad():
            start_time = time.time()
            
            # Initialize compiled model on first inference
            if self.compiled_policy is None:
                self.get_logger().info("Compiling the policy model with float16 support...")
                try:
                    # First trace the model with float16 inputs
                    # Make sure inputs are explicitly half precision
                    example_qpos = qpos_tensor.clone().detach().half()
                    example_images = images.clone().detach().half()
                    
                    # Trace the model with half precision inputs
                    scripted = torch.jit.trace(self.policy, (example_qpos, example_images))
                    
                    # Then compile for low overhead with float16 support
                    self.compiled_policy = torch.compile(
                        scripted,
                        backend="inductor",
                        mode="reduce-overhead"  # minimize launch checks
                    )
                    self.get_logger().info("Model compilation with float16 support successful!")
                except Exception as e:
                    self.get_logger().error(f"Model compilation failed: {e}")
                    self.compiled_policy = self.policy  # Fallback to original model
            
            # Use the compiled model for inference
            output = self.compiled_policy(qpos_tensor, images)
            
            self.get_logger().info(f"Policy time: {time.time() - start_time:.3f} seconds")
            if self.norm_stats is not None and "action_mean" in self.norm_stats:
                action_mean = torch.tensor(self.norm_stats["action_mean"], dtype=torch.float16, device=self.device)
                action_std = torch.tensor(self.norm_stats["action_std"], dtype=torch.float16, device=self.device)
                unnormalized_actions = output * action_std + action_mean
                # Here, we use the first 10 predicted actions as our chunk
                actions = unnormalized_actions[:, :CHUNK_SIZE, :].cpu()
                # self.get_logger().info(f"actions: {actions.shape}")
                return actions
            else:
                return None

    ####################################################
    # Inference Loop
    ####################################################
    def inference_loop(self):
        start_time = time.time()
        # If the action buffer is empty, run inference to get a new sequence of actions.
        if not self.action_buffer:
            actions = self.run_inference()
            if actions is None:
                return

            if USE_TEMPORAL_ENSEMBLING:
                # Convert the list of predicted actions into a tensor of shape (1, chunk_size, action_dim)
                # Update the temporal ensembler with the new chunk.
                ensembled_action_tensor = self.temporal_ensembler.update(actions)
                # self.get_logger().info(f"ensembled_action_tensor: {ensembled_action_tensor.shape}")
                ensembled_action = ensembled_action_tensor.squeeze(0).cpu().numpy().tolist()

                # In temporal ensembling mode, we produce one ensembled action per inference cycle.
                self.action_buffer.append(ensembled_action)
            else:
                # In raw mode, fill the buffer with all predicted actions.
                self.action_buffer = actions.squeeze(0).cpu().numpy().tolist()
                # self.get_logger().info("New action buffer computed with raw predictions.")

        # Pop the next action from the buffer and publish it.
        next_action = self.action_buffer.pop(0)
        # self.get_logger().info(f"next_action: {next_action}")

        # Extract and publish the twist command (last two elements).
        twist_msg = Twist()
        twist_msg.linear.x = next_action[-2] / 2
        twist_msg.angular.z = next_action[-1] / 2
        self.cmd_vel_pub.publish(twist_msg)

        # Extract and publish the arm command (first six elements).
        arm_msg = Float64MultiArray()
        arm_msg.data = next_action[:6]
        self.arm_state_pub.publish(arm_msg)

        # self.get_logger().info(f"Inference cycle time: {time.time() - start_time:.3f} seconds")

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
