#!/usr/bin/env python3

"""
A ROS2 Humble node that indexes a data directory for task subdirectories
(with valid metadata) and displays a numbered list. The user selects a task,
and the node visualizes each episode (showing images at 30 Hz with overlaid arm
positions and velocities). Left/right arrow keys allow switching tasks.
"""

import os
import json
import h5py
import numpy as np
import cv2
import rclpy
from rclpy.node import Node

# Note: Adjust these key codes if necessary.
LEFT_ARROW = 81    # may vary per system
RIGHT_ARROW = 83   # may vary per system

class DataViewerNode(Node):
    def __init__(self):
        super().__init__('data_viewer')
        # Declare parameters with defaults
        self.declare_parameter('data_directory', '/home/vignesh/maurice-prod/data')
        self.declare_parameter('data_frequency', 10)
        self.declare_parameter('image_topics', ["/color/image", "/image_raw"])
        self.declare_parameter('arm_state_topic', "/arm/state")
        self.declare_parameter('leader_command_topic', "/leader/command")
        self.declare_parameter('velocity_topic', "/cmd_vel")

        # Retrieve all parameters
        self.base_data_directory = self.get_parameter('data_directory').value
        self.data_frequency = self.get_parameter('data_frequency').value
        self.image_topics = self.get_parameter('image_topics').value
        self.arm_state_topic = self.get_parameter('arm_state_topic').value
        self.leader_command_topic = self.get_parameter('leader_command_topic').value
        self.velocity_topic = self.get_parameter('velocity_topic').value

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
        self.visualize_task()  # start visualization for the selected task

    def index_tasks(self):
        """
        Walk through the base directory and return a list of tasks.
        Each task is a dictionary with task name, directory, metadata and episode count.
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

    def visualize_task(self):
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

            print("Press left/right arrow keys to navigate episodes, or 'q' to quit.")
            key = cv2.waitKey(0)
            if key == ord('q'):
                self.get_logger().info("Quitting viewer.")
                rclpy.shutdown()
                break
            elif key == LEFT_ARROW:
                current_episode_index = (current_episode_index - 1) % len(episodes)
            elif key == RIGHT_ARROW:
                current_episode_index = (current_episode_index + 1) % len(episodes)

    def play_episode(self, file_path):
        """
        Open an episode HDF5 file, and play back each time step at ~30 Hz.
        Overlays the arm positions (qpos) and velocities (qvel) onto the displayed images.
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
                # Get t-th image for each camera.
                img = images_group[cam][t]
                # If necessary, convert image color (e.g. from RGB to BGR for OpenCV).
                if len(img.shape) == 3 and img.shape[2] == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                # Resize image to target dimensions
                img = cv2.resize(img, (1280, 720))
                img_list.append(img)

            # Combine images side-by-side (if more than one camera).
            if img_list:
                try:
                    disp_img = cv2.hconcat(img_list)
                except Exception:
                    disp_img = img_list[0]
            else:
                disp_img = np.zeros((480, 640, 3), dtype=np.uint8)

            # Overlay arm state info.
            overlay_text = f"qpos: {qpos[t]} \n qvel: {qvel[t]}"
            cv2.putText(disp_img, overlay_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 0), 2)

            cv2.imshow("Episode Playback", disp_img)
            # Wait ~33 ms (30 Hz). Also capture key events.
            key = cv2.waitKey(33)  # key is an integer code

            # Allow quitting early if user presses 'q'
            if key == ord('q'):
                break
            # Check for arrow keys to switch tasks mid-episode
            if key == LEFT_ARROW:
                self.current_task_index = (self.current_task_index - 1) % len(self.tasks)
                self.get_logger().info("Switching to previous task")
                hf.close()
                cv2.destroyAllWindows()
                self.visualize_task()
                return
            elif key == RIGHT_ARROW:
                self.current_task_index = (self.current_task_index + 1) % len(self.tasks)
                self.get_logger().info("Switching to next task")
                hf.close()
                cv2.destroyAllWindows()
                self.visualize_task()
                return

        hf.close()
        cv2.destroyAllWindows()

    def wait_for_task_navigation(self):
        """
        After an entire task has been visualized, wait for the user to press
        the left/right arrow key to change tasks or 'q' to quit.
        """
        print("Press left/right arrow keys to switch tasks, or 'q' to quit.")
        while True:
            key = cv2.waitKey(0)
            if key == ord('q'):
                self.get_logger().info("Quitting viewer.")
                rclpy.shutdown()
                break
            elif key == LEFT_ARROW:
                self.current_task_index = (self.current_task_index - 1) % len(self.tasks)
                self.visualize_task()
                break
            elif key == RIGHT_ARROW:
                self.current_task_index = (self.current_task_index + 1) % len(self.tasks)
                self.visualize_task()
                break


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
