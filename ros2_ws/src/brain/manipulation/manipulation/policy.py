#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image, JointState
from cv_bridge import CvBridge
import numpy as np
import torch
import torchvision.transforms as transforms

# Import your policy class and any necessary constants/configs.
from InnateACT.policy import ACTPolicy

# Define the policy configuration (make sure these match your training configuration)
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
        self.get_logger().info("Inference node started.")
        self.bridge = CvBridge()

        # Set device and load the policy model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.policy = ACTPolicy(policy_config).to(self.device)
        checkpoint_path = '/home/vignesh/maurice-prod/ros2_ws/src/brain/manipulation/ckpts/Paper_20250303_1859/policy_epoch_16000_seed_100.ckpt'  # Update this path to your checkpoint file
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
        self.create_subscription(Image, '/camera/image_raw', self.image1_callback, 10)
        self.create_subscription(Image, '/camera/image_processed', self.image2_callback, 10)
        self.create_subscription(JointState, '/arm/state', self.joint_state_callback, 10)

        # Timer to run the inference loop at 10 Hz
        self.timer = self.create_timer(0.1, self.inference_loop)

    def image1_callback(self, msg: Image):
        try:
            self.latest_image1 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().error(f"Error converting image1: {e}")

    def image2_callback(self, msg: Image):
        try:
            self.latest_image2 = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().error(f"Error converting image2: {e}")

    def joint_state_callback(self, msg: JointState):
        self.latest_joint_state = msg

    def inference_loop(self):
        # Ensure that data has been received from all topics
        if self.latest_image1 is None or self.latest_image2 is None or self.latest_joint_state is None:
            self.get_logger().info("Waiting for all topics to be received...")
            return

        self.get_logger().info("Running inference...")

        # Preprocess the images: convert to float, normalize, and change channel order from HWC to CHW.
        try:
            img1 = self.latest_image1.astype(np.float32) / 255.0
            img2 = self.latest_image2.astype(np.float32) / 255.0
            img1 = np.transpose(img1, (2, 0, 1))
            img2 = np.transpose(img2, (2, 0, 1))
        except Exception as e:
            self.get_logger().error(f"Error processing images: {e}")
            return

        # Convert the images to torch tensors and apply normalization
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])
        try:
            img1_tensor = torch.tensor(img1).to(self.device)
            img2_tensor = torch.tensor(img2).to(self.device)
            img1_tensor = normalize(img1_tensor)
            img2_tensor = normalize(img2_tensor)
        except Exception as e:
            self.get_logger().error(f"Error normalizing images: {e}")
            return

        # Stack images along a new dimension and add batch dimension: expected shape [B, Cams, C, H, W]
        images = torch.stack([img1_tensor, img2_tensor], dim=0).unsqueeze(0)

        # Process the joint state to obtain qpos tensor
        try:
            qpos = np.array(self.latest_joint_state.position, dtype=np.float32)
            qpos_tensor = torch.tensor(qpos).unsqueeze(0).to(self.device)
        except Exception as e:
            self.get_logger().error(f"Error processing joint state: {e}")
            return

        # For inference, pass qpos and image; actions is None so the policy will sample from the prior.
        with torch.no_grad():
            output = self.policy(qpos_tensor, images)
            self.get_logger().info(str(output.shape))

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
