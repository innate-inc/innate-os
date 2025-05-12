#!/usr/bin/env python3
"""
A ROS2 Humble node that indexes a data directory for task subdirectories
(with valid metadata) and displays a numbered list. The user selects a task,
and the node visualizes one episode at a time (showing images at ~30 Hz with overlaid
arm positions and velocities). Left/right arrow keys switch episodes and 'q' quits.
"""

import os
import json
import h5py
import numpy as np
import cv2
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
import time
from tf2_ros import TransformBroadcaster
from geometry_msgs.msg import TransformStamped

# Key codes for Linux (if your tests show left arrow=81 and right arrow=83, use these)
LEFT_ARROW = 81
RIGHT_ARROW = 83

class DataViewerNode(Node):
    def __init__(self):
        super().__init__('data_viewer')
        self.get_logger().info("Initializing DataViewerNode...")
        
        # Declare parameters with defaults
        self.declare_parameter('data_frequency', 30)
        self.declare_parameter('image_size', [640, 480])
        
        # Create publishers for robot state
        self.get_logger().info("Creating joint state publisher...")
        self.joint_state_pub = self.create_publisher(JointState, '/joint_states', 10)
        self.joint_names = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']
        
        # Retrieve parameters
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_size = self.get_parameter('image_size').value

        # Get task directory via GUI
        self.task_directory = self.prompt_directory_selection_gui()
        if not self.task_directory:
            self.get_logger().error("No directory selected. Exiting...")
            rclpy.shutdown()
            return

        # Load metadata
        metadata_path = os.path.join(self.task_directory, "metadata.json")
        if not os.path.exists(metadata_path):
            self.get_logger().error(f"No metadata.json found in {self.task_directory}")
            rclpy.shutdown()
            return

        try:
            with open(metadata_path, 'r') as f:
                self.metadata = json.load(f)
            self.episodes = self.metadata.get("episodes", [])
            if not self.episodes:
                self.get_logger().error("No episodes found in metadata")
                rclpy.shutdown()
                return
            self.get_logger().info(f"Loaded {len(self.episodes)} episodes from {self.metadata.get('task_name', 'Unknown Task')}")
        except Exception as e:
            self.get_logger().error(f"Failed to load metadata: {e}")
            rclpy.shutdown()
            return

        # Create a timer to start visualization after a short delay
        self.create_timer(1.0, self.start_visualization)
        
        self.tf_broadcaster = TransformBroadcaster(self)

    def visualize_episodes(self):
        """
        Visualize one episode at a time for the selected task.
        Left/right arrow keys switch episodes, and 'q' quits.
        """
        self.get_logger().info("Starting visualize_episodes...")
        current_episode_index = 0

        while True:
            self.get_logger().info(f"Processing episode {current_episode_index + 1}")
            episode = self.episodes[current_episode_index]
            file_name = episode.get("file_name")
            file_path = os.path.join(self.task_directory, file_name)
            self.get_logger().info(
                f"Playing episode {episode.get('episode_id')} ({current_episode_index+1}/{len(self.episodes)}): {file_name}"
            )
            self.play_episode(file_path)

            # Create a dummy window to wait for key input.
            self.get_logger().info("Waiting for key input...")
            cv2.namedWindow("Episode Navigation", cv2.WINDOW_NORMAL)
            print("Press left/right arrow keys to navigate episodes, or 'q' to quit.")
            key = cv2.waitKey(0)
            cv2.destroyWindow("Episode Navigation")

            if key == ord('q'):
                self.get_logger().info("Quitting viewer.")
                break
            elif key == LEFT_ARROW:
                current_episode_index = (current_episode_index - 1) % len(self.episodes)
                self.get_logger().info(f"Moving to previous episode: {current_episode_index + 1}")
            elif key == RIGHT_ARROW:
                current_episode_index = (current_episode_index + 1) % len(self.episodes)
                self.get_logger().info(f"Moving to next episode: {current_episode_index + 1}")
            # Otherwise, repeat the same episode

        cv2.destroyAllWindows()
        rclpy.shutdown()

    def broadcast_tf(self, qpos):
        """Broadcast transforms for the robot's base and links"""
        # Base transform
        t = TransformStamped()
        t.header.stamp = self.get_clock().now().to_msg()
        t.header.frame_id = "base_root"
        t.child_frame_id = "base_link"
        t.transform.translation.x = 0.0
        t.transform.translation.y = 0.0
        t.transform.translation.z = 0.0
        t.transform.rotation.w = 1.0
        self.tf_broadcaster.sendTransform(t)
        
        # Add more transforms for other links as needed
        
    def play_episode(self, file_path):
        """
        Open an episode HDF5 file and play back each time step at ~30 Hz.
        Overlays the arm positions (qpos) and velocities (qvel) onto the displayed images.
        During playback, only 'q' is handled to allow quitting early.
        """
        self.get_logger().info(f"Opening file: {file_path}")
        try:
            hf = h5py.File(file_path, 'r')
        except Exception as e:
            self.get_logger().error(f"Failed to open file {file_path}: {e}")
            return

        self.get_logger().info("Loading data from HDF5 file...")
        actions = np.array(hf['/action'])
        qpos = np.array(hf['/observations/qpos'])
        qvel = np.array(hf['/observations/qvel'])
        images_group = hf['/observations/images']
        camera_names = list(images_group.keys())
        num_timesteps = actions.shape[0]
        self.get_logger().info(f"Loaded {num_timesteps} timesteps")

        # Create window once at the start
        cv2.namedWindow("Episode Playback", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("Episode Playback", cv2.WND_PROP_TOPMOST, 1)
        cv2.moveWindow("Episode Playback", 100, 100)

        try:
            for t in range(num_timesteps):
                # Broadcast transforms
                self.broadcast_tf(qpos[t])

                # Publish joint states for RViz visualization
                joint_state_msg = JointState()
                joint_state_msg.header.stamp = self.get_clock().now().to_msg()
                joint_state_msg.name = self.joint_names
                joint_state_msg.position = qpos[t].tolist()
                joint_state_msg.velocity = qvel[t].tolist()
                self.joint_state_pub.publish(joint_state_msg)

                img_list = []
                for cam in camera_names:
                    img = images_group[cam][t]
                    if len(img.shape) == 3 and img.shape[2] == 3:
                        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                    img = cv2.resize(img, (self.image_size[0], self.image_size[1]))
                    img_list.append(img)

                if img_list:
                    try:
                        disp_img = cv2.hconcat(img_list)
                    except Exception:
                        disp_img = img_list[0]
                else:
                    disp_img = np.zeros((480, 640, 3), dtype=np.uint8)

                # Set font parameters
                font_scale = 0.5
                thickness = 1
                line_height = 20
                x, y0 = 10, 30

                # Draw three lines of text
                cv2.putText(disp_img, f"qpos: {qpos[t]}", (x, y0), cv2.FONT_HERSHEY_SIMPLEX, 
                           font_scale, (0, 255, 0), thickness)
                cv2.putText(disp_img, f"cmd_vel: {qvel[t]}", (x, y0 + line_height), 
                           cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 255, 0), thickness)
                cv2.putText(disp_img, f"action: {actions[t]}", (x, y0 + 2*line_height), 
                           cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 255, 0), thickness)

                # Add a visual indicator that window is active
                cv2.putText(disp_img, "Window Active - Press 'q' to quit", (10, disp_img.shape[0] - 10),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                
                cv2.imshow("Episode Playback", disp_img)
                key = cv2.waitKey(int(1000/self.data_frequency))
                if key == ord('q'):
                    break

                # Add a small delay to maintain the desired frequency
                time.sleep(1.0/self.data_frequency)

                if t % 30 == 0:  # Log every 30 frames to avoid too much output
                    self.get_logger().info(f"Processed frame {t}/{num_timesteps}")

        finally:
            # Clean up
            cv2.destroyWindow("Episode Playback")
            hf.close()

        self.get_logger().info("Finished playing episode")

    def prompt_directory_selection_gui(self):
        """
        Opens a GUI dialog for the user to select a directory.
        Returns the selected path or None if cancelled or an error occurs.
        """
        try:
            import tkinter as tk
            from tkinter import filedialog
            
            # Set up the root window
            root = tk.Tk()
            root.withdraw()  # Hide the main tkinter window
            root.attributes('-topmost', True) # Make the dialog appear on top of other windows

            # Open the directory selection dialog
            self.get_logger().info("Opening file explorer to select data directory...")
            dir_path = filedialog.askdirectory(
                title="Select Data Directory",
                initialdir=os.path.expanduser("~") # Start in user's home directory
            )
            
            root.destroy() # Clean up the tkinter root window

            if dir_path:
                self.get_logger().info(f"Directory selected via GUI: {dir_path}")
                return os.path.expanduser(dir_path)
            else:
                self.get_logger().info("Directory selection via GUI was cancelled.")
                return None
        except ImportError:
            self.get_logger().warn(
                "tkinter library not found. Cannot show GUI for directory selection. "
                "Please install it (e.g., 'sudo apt-get install python3-tk'). "
                "Falling back to ROS parameter for data directory."
            )
            return None
        except Exception as e:
            self.get_logger().error(f"Error during GUI directory selection: {e}")
            return None

    def start_visualization(self):
        """Start the visualization process"""
        self.visualize_episodes()

def main(args=None):
    rclpy.init(args=args)
    node = DataViewerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()

