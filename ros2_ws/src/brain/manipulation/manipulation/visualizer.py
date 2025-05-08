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

# Key codes for Linux (if your tests show left arrow=81 and right arrow=83, use these)
LEFT_ARROW = 81
RIGHT_ARROW = 83

class DataViewerNode(Node):
    def __init__(self):
        super().__init__('data_viewer')
        # Declare parameters with defaults
        self.declare_parameter('data_directory', '/home/vignesh/maurice-prod/data')
        self.declare_parameter('data_frequency', 30)
        self.declare_parameter('image_topics', ["/color/image", "/image_raw"])
        self.declare_parameter('arm_state_topic', "/arm/state")
        self.declare_parameter('leader_command_topic', "/leader/command")
        self.declare_parameter('velocity_topic', "/cmd_vel")
        self.declare_parameter('image_size', [640, 480])
        # Retrieve parameters
        self.base_data_directory = os.path.expanduser(self.get_parameter('data_directory').value)
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_topics = self.get_parameter('image_topics').value
        self.arm_state_topic = self.get_parameter('arm_state_topic').value
        self.leader_command_topic = self.get_parameter('leader_command_topic').value
        self.velocity_topic = self.get_parameter('velocity_topic').value
        self.image_size = self.get_parameter('image_size').value
        self.get_logger().info(f"Using data directory: {self.base_data_directory}")
        self.get_logger().info(f"Playback frequency: {self.data_frequency} Hz")

        # Index available tasks (subdirectories with metadata.json)
        self.tasks = self.index_tasks()
        if not self.tasks:
            self.get_logger().info(f"No valid task directories found in {self.base_data_directory}.")
            rclpy.shutdown()
            return

        # List tasks and ask the user for a selection.
        self.current_task_index = self.select_task()
        self.visualize_episodes()  # Start episode visualization for the selected task

    def index_tasks(self):
        """
        Walk through the base directory and return a list of tasks.
        Each task is a dictionary with task name, directory, metadata, and episode count.
        """
        tasks = []
        for subdir in os.listdir(self.base_data_directory):
            task_dir = os.path.join(self.base_data_directory, subdir)
            if os.path.isdir(task_dir):
                metadata_path = os.path.join(task_dir, "metadata.json")
                if os.path.exists(metadata_path):
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                    num_episodes = metadata.get("number_of_episodes", len(metadata.get("episodes", [])))
                    tasks.append({
                        'task_name': metadata.get("task_name", subdir),
                        'directory': task_dir,
                        'metadata': metadata,
                        'num_episodes': num_episodes
                    })
        # Print out the found tasks with numbers.
        for i, task in enumerate(tasks, start=1):
            print(f"{i}: Task '{task['task_name']}' with {task['num_episodes']} episodes")
        return tasks

    def select_task(self):
        """
        Ask the user to input a task number until a valid selection is made.
        """
        selection = None
        while selection is None:
            try:
                inp = input("Enter task number to visualize: ")
                selection = int(inp)
                if selection < 1 or selection > len(self.tasks):
                    print("Invalid selection. Try again.")
                    selection = None
            except ValueError:
                print("Please enter a valid number.")
        # Convert to a zero-index.
        return selection - 1

    def flush_key_buffer(self):
        """Flush any pending key events."""
        while cv2.waitKey(1) != -1:
            pass

    def visualize_episodes(self):
        """
        Visualize one episode at a time for the selected task.
        Left/right arrow keys switch episodes, and 'q' quits.
        """
        task = self.tasks[self.current_task_index]
        self.get_logger().info(f"Visualizing task: {task['task_name']}")
        episodes = task['metadata'].get("episodes", [])
        if not episodes:
            self.get_logger().warn("No episodes found for this task.")
            return

        current_episode_index = 0

        while True:
            episode = episodes[current_episode_index]
            file_name = episode.get("file_name")
            file_path = os.path.join(task['directory'], file_name)
            self.get_logger().info(
                f"Playing episode {episode.get('episode_id')} ({current_episode_index+1}/{len(episodes)}): {file_name}"
            )
            self.play_episode(file_path)

            # Create a dummy window to wait for key input.
            cv2.namedWindow("Episode Navigation", cv2.WINDOW_NORMAL)
            print("Press left/right arrow keys to navigate episodes, or 'q' to quit.")
            key = cv2.waitKey(0)
            cv2.destroyWindow("Episode Navigation")

            if key == ord('q'):
                self.get_logger().info("Quitting viewer.")
                break
            elif key == LEFT_ARROW:
                current_episode_index = (current_episode_index - 1) % len(episodes)
            elif key == RIGHT_ARROW:
                current_episode_index = (current_episode_index + 1) % len(episodes)
            # Otherwise, repeat the same episode

        cv2.destroyAllWindows()
        rclpy.shutdown()

    def play_episode(self, file_path):
        """
        Open an episode HDF5 file and play back each time step at ~30 Hz.
        Overlays the arm positions (qpos) and velocities (qvel) onto the displayed images.
        During playback, only 'q' is handled to allow quitting early.
        """
        try:
            hf = h5py.File(file_path, 'r')
        except Exception as e:
            self.get_logger().error(f"Failed to open file {file_path}: {e}")
            return

        actions = np.array(hf['/action'])
        qpos = np.array(hf['/observations/qpos'])
        qvel = np.array(hf['/observations/qvel'])
        images_group = hf['/observations/images']
        camera_names = list(images_group.keys())
        num_timesteps = actions.shape[0]

        for t in range(num_timesteps):
            img_list = []
            for cam in camera_names:
                # Get the t-th image for each camera.
                img = images_group[cam][t]
                # Convert image color if needed.
                if len(img.shape) == 3 and img.shape[2] == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                # Resize to common dimensions (e.g., 640x480)
                img = cv2.resize(img, (self.image_size[0], self.image_size[1]))
                img_list.append(img)

            # Combine images side-by-side.
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

            cv2.imshow("Episode Playback", disp_img)
            key = cv2.waitKey(int(1000/self.data_frequency))  # ~30 Hz playback
            if key == ord('q'):
                break

        hf.close()
        cv2.destroyAllWindows()

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

