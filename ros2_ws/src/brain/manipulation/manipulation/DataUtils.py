#!/usr/bin/env python3
# File: /ros2_ws/src/brain/manipulation/manipulation/DataUtils.py

import h5py
import numpy as np
import os
import json
from brain_messages.msg import RecorderStatus

class EpisodeData:
    def __init__(self, camera_names=None):
        """
        Initialize the EpisodeData instance.
        
        Args:
            camera_names (list, optional): List of camera names.
                If not provided, will be set dynamically on first add_timestep call.
        """
        self.camera_names = camera_names
        
        # Initialize buffers for time-step data
        self.actions = []
        self.qpos = []
        self.qvel = []
        
        # Initialize images dict only if camera_names is provided
        if camera_names is not None:
            self.images = {cam: [] for cam in self.camera_names}
        else:
            self.images = {}
    
    def add_timestep(self, action, qpos, qvel, images):
        """
        Add a new time step of data.
        
        On the first call, it dynamically sets the camera configuration based on the provided images.
        Subsequent calls must match the initial camera configuration.
        
        Args:
            action: Data representing the action at the current time step.
            qpos: Data representing the robot's position at the current time step.
            qvel: Data representing the robot's velocity at the current time step.
            images (list): A list of images.
        
        Raises:
            ValueError: If a subsequent call does not provide the same number of images as initially determined.
        """
        if self.camera_names is None:
            # Dynamically set camera names based on the number of images in the first timestep
            self.camera_names = [f"camera_{i+1}" for i in range(len(images))]
            self.images = {cam: [] for cam in self.camera_names}
        elif len(images) != len(self.camera_names):
            raise ValueError(f"Expected {len(self.camera_names)} images, but got {len(images)}")
        
        self.actions.append(action)
        self.qpos.append(qpos)
        self.qvel.append(qvel)
        
        # Append each image to the appropriate camera's list
        for idx, cam in enumerate(self.camera_names):
            self.images[cam].append(images[idx])
    
    def save_file(self, path):
        """
        Save the buffered episode data into an HDF5 file at the specified path.
        
        The file will have the following structure:
        
            /action         -> Dataset containing the action data.
            /observations
                ├── qpos  -> Dataset containing the qpos data.
                ├── qvel  -> Dataset containing the qvel data.
                └── images
                      ├── camera1 -> Dataset containing images for camera1.
                      ├── camera2 -> Dataset containing images for camera2.
                      └── ...     -> For additional cameras.
        
        Args:
            path (str): Full file path (including filename, e.g., 'episode_1.h5').
        """
        with h5py.File(path, 'w') as hf:
            # Create dataset for actions.
            hf.create_dataset('/action', data=np.array(self.actions))
            
            # Create the observations group.
            obs_group = hf.create_group('/observations')
            obs_group.create_dataset('qpos', data=np.array(self.qpos))
            obs_group.create_dataset('qvel', data=np.array(self.qvel))
            
            # Create a subgroup for images.
            images_group = obs_group.create_group('images')
            for cam in self.camera_names:
                # Convert the list of images to a numpy array.
                # Assumes that all images for a given camera have consistent shape.
                images_group.create_dataset(cam, data=np.array(self.images[cam]))
    
    def clear(self):
        """
        Clear all buffered data.
        
        This method resets the buffers for actions, qpos, qvel, and images,
        maintaining the same camera configuration as before.
        """
        self.actions = []
        self.qpos = []
        self.qvel = []
        self.images = {cam: [] for cam in self.camera_names} if self.camera_names else {}
    
    def get_episode_length(self):
        """
        Return the number of time steps recorded in this episode.
        
        Assumes that all data buffers (actions, qpos, qvel) have the same length.
        
        Returns:
            int: The number of time steps.
        """
        return len(self.actions)
    
    def get_data(self):
        """
        Retrieve the buffered data as a dictionary.
        
        Returns:
            dict: Contains 'actions', 'qpos', 'qvel', and 'images' buffers.
        """
        return {
            'actions': self.actions,
            'qpos': self.qpos,
            'qvel': self.qvel,
            'images': self.images
        }


class TaskManager:
    def __init__(self, base_data_directory):
        """
        Initialize the TaskManager.

        Args:
            base_data_directory (str): The root directory where all task directories are created.
        """
        self.base_data_directory = base_data_directory
        self.current_task_name = None
        self.current_task_dir = None
        self.metadata = None  # Will hold the task metadata
        self.episodes = []    # List of EpisodeData objects

    def start_new_task(self, task_name, task_description, mobile_flag, data_frequency):
        """
        Start a new task by creating a task directory and initializing metadata.
        If a task with the given name already exists (i.e., a metadata file is found),
        the task will be resumed instead.

        Args:
            task_name (str): The name for the new task.
            task_description (str): A description for the task.
            mobile_flag (bool): Indicates if the task involves mobile data.
            data_frequency (float): The frequency at which data is collected (in Hz).
        """
        self.current_task_name = task_name
        self.current_task_dir = os.path.join(self.base_data_directory, task_name)
        metadata_path = os.path.join(self.current_task_dir, "metadata.json")

        if os.path.exists(metadata_path):
            # Task already exists; resume it.
            print(f"Task '{task_name}' already exists. Resuming task.")
            self.resume_task(task_name)
            return

        # Task does not exist; create a new one.
        os.makedirs(self.current_task_dir, exist_ok=True)
        self.metadata = {
            "task_name": task_name,
            "task_description": task_description,
            "mobile_task": mobile_flag,
            "data_frequency": data_frequency,
            "number_of_episodes": 0,
            "episodes": []  # Will contain details for each saved episode.
        }
        self._save_metadata()
        self.episodes = []  # Reset the episodes list.

    def resume_task(self, task_name):
        """
        Resume a previously started task by loading its metadata and setting the current task context.
        
        Args:
            task_name (str): The name of the task to resume.
        
        Raises:
            FileNotFoundError: If the task directory or metadata file does not exist.
        """
        self.current_task_name = task_name
        self.current_task_dir = os.path.join(self.base_data_directory, task_name)
        metadata_path = os.path.join(self.current_task_dir, "metadata.json")
        if not os.path.exists(metadata_path):
            raise FileNotFoundError(f"Metadata file not found for task '{task_name}' at {self.current_task_dir}")
        self.load_metadata()
        # Optionally, the episodes list can be updated if needed by reading from the HDF5 files.
        # For now, we leave self.episodes as an empty list.
    
    def add_episode(self, episode_data, start_timestamp, end_timestamp):
        """
        Save an episode's data to an HDF5 file and update the task's metadata.

        Args:
            episode_data (EpisodeData): The EpisodeData object containing buffered data.
            start_timestamp (str): Start timestamp of the episode (e.g., ISO format).
            end_timestamp (str): End timestamp of the episode.
        """
        # Determine new episode ID and filename.
        episode_id = self.metadata["number_of_episodes"]
        file_name = f"episode_{episode_id}.h5"
        file_path = os.path.join(self.current_task_dir, file_name)
        
        # Save the episode HDF5 file.
        episode_data.save_file(file_path)
        
        # Update metadata with new episode info.
        episode_info = {
            "episode_id": episode_id,
            "file_name": file_name,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp
        }
        self.metadata["episodes"].append(episode_info)
        self.metadata["number_of_episodes"] += 1
        self._save_metadata()
        
        # Optionally, store the episode_data object.
        #self.episodes.append(episode_data)

    def end_task(self):
        """
        End the current task by finalizing metadata and resetting state.
        """
        self._save_metadata()
        self.current_task_name = None
        self.current_task_dir = None
        self.metadata = None
        self.episodes = []

    def _save_metadata(self):
        """
        Save the current metadata to a JSON file in the task directory.
        """
        if self.current_task_dir is None:
            raise RuntimeError("No active task directory to save metadata.")
        metadata_path = os.path.join(self.current_task_dir, "metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=4)

    def load_metadata(self):
        """
        Load the metadata from the JSON file in the current task directory.
        """
        if self.current_task_dir is None:
            raise RuntimeError("No active task directory to load metadata from.")
        metadata_path = os.path.join(self.current_task_dir, "metadata.json")
        with open(metadata_path, 'r') as f:
            self.metadata = json.load(f)

    def get_status_message(self, episode_number, status):
        """
        Create and return a RecorderStatus message containing the current task name,
        the given episode number, and a custom status message.

        Args:
            episode_number (str): The episode number as a string (or "N/A" if not applicable).
            status (str): A status message describing the current state.

        Returns:
            RecorderStatus: The constructed RecorderStatus message.
        """
        msg = RecorderStatus()
        msg.current_task_name = self.current_task_name if self.current_task_name else ""
        msg.episode_number = episode_number
        msg.status = status
        return msg

    def _get_enriched_metadata_for_task(self, task_directory):
        """
        Loads, enriches (with episode num_timesteps), and returns metadata for a single task
        using its absolute directory path.

        Args:
            task_directory (str): Absolute path to the task directory.

        Returns:
            tuple: (dict_or_None, error_message_or_None)
                   - dict_or_None: The enriched task metadata.
                   - error_message_or_None: Description of error if any.
        """
        metadata_file_path = os.path.join(task_directory, "metadata.json")
        # Extract task name from directory path for fallback if not in metadata
        task_name_for_fallback = os.path.basename(task_directory) 

        if not os.path.exists(task_directory) or not os.path.isdir(task_directory):
            return None, f"Task directory {task_directory} not found or is not a directory."
        
        if not os.path.exists(metadata_file_path):
            return None, f"Metadata.json not found in {task_directory}."

        try:
            with open(metadata_file_path, 'r') as f:
                task_metadata = json.load(f)
        except json.JSONDecodeError as e:
            return None, f"Error decoding metadata.json in {task_directory}: {e}"
        except Exception as e:
            return None, f"Error reading metadata.json in {task_directory}: {e}"

        processed_episodes = []
        if "episodes" in task_metadata and isinstance(task_metadata["episodes"], list):
            for episode_info in task_metadata["episodes"]:
                num_timesteps = 0
                episode_file_name = episode_info.get("file_name", "")
                episode_file_path = os.path.join(task_directory, episode_file_name)
                
                if episode_file_name and os.path.exists(episode_file_path):
                    try:
                        with h5py.File(episode_file_path, 'r') as hf:
                            if '/action' in hf:
                                num_timesteps = len(hf['/action'])
                    except Exception as e:
                        print(f"Error reading HDF5 file {episode_file_path} for timesteps: {e}")
                
                processed_episodes.append({
                    "episode_id": f"episode_{episode_info.get('episode_id', 'N/A')}",
                    "start_time": episode_info.get("start_timestamp", "N/A"),
                    "end_time": episode_info.get("end_timestamp", "N/A"),
                    "num_timesteps": num_timesteps,
                    "data_file_name": episode_file_name
                })
        
        enriched_metadata = {
            "task_name": task_metadata.get("task_name", task_name_for_fallback),
            "task_description": task_metadata.get("task_description", "N/A"),
            "mobile_task": task_metadata.get("mobile_task", False),
            "data_frequency": task_metadata.get("data_frequency", 0),
            "task_directory": task_directory, 
            "episodes": processed_episodes,
            **{k: v for k, v in task_metadata.items() if k not in ["task_name", "task_description", "mobile_task", "data_frequency", "task_directory", "episodes"]}
        }
        
        return enriched_metadata, None

    def get_all_tasks_summary(self):
        """
        Scans the base_data_directory for all tasks and compiles a summary
        of each task and its episodes, including the number of timesteps per episode.
        Uses the _get_enriched_metadata_for_task helper.
        """
        all_tasks_summary = []
        if not os.path.exists(self.base_data_directory) or not os.path.isdir(self.base_data_directory):
            print(f"Base data directory {self.base_data_directory} does not exist or is not a directory.")
            return all_tasks_summary

        for task_dir_name in os.listdir(self.base_data_directory):
            current_task_directory = os.path.join(self.base_data_directory, task_dir_name)
            if not os.path.isdir(current_task_directory): 
                continue

            metadata_obj, error_msg = self._get_enriched_metadata_for_task(current_task_directory)
            
            if metadata_obj:
                all_tasks_summary.append(metadata_obj)
            elif error_msg:
                print(f"Skipping task directory {task_dir_name} due to error: {error_msg}")
                
        return all_tasks_summary

    def update_task_metadata_by_directory(self, task_directory: str, json_metadata_update: str):
        """
        Updates the metadata for a given task using its absolute directory path.

        Args:
            task_directory (str): The absolute path of the task directory to update.
            json_metadata_update (str): A JSON string containing the metadata fields to update.

        Returns:
            tuple: (bool, str) indicating success status and a message.
        """
        metadata_file_path = os.path.join(task_directory, 'metadata.json')

        if not os.path.exists(task_directory) or not os.path.isdir(task_directory):
            return False, f"Task directory '{task_directory}' not found or is not a directory."
        
        if not os.path.exists(metadata_file_path):
            return False, f"Metadata file for task at '{task_directory}' not found."

        try:
            update_data = json.loads(json_metadata_update)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON format in update: {str(e)}"

        try:
            with open(metadata_file_path, 'r+') as f:
                metadata = json.load(f)
                for key, value in update_data.items():
                    metadata[key] = value
                
                f.seek(0)  
                json.dump(metadata, f, indent=4)
                f.truncate() 
            return True, "Metadata updated successfully."
        except Exception as e:
            return False, f"Failed to read or write metadata at {task_directory}: {str(e)}"

    def get_task_metadata_by_directory(self, task_directory: str):
        """
        Retrieves the enriched metadata for a specific task using its absolute directory path.
        Uses the _get_enriched_metadata_for_task helper.

        Args:
            task_directory (str): The absolute path to the task directory.

        Returns:
            tuple: (bool, str, str) with success, message, and JSON metadata.
        """
        metadata_obj, error_msg = self._get_enriched_metadata_for_task(task_directory)
        
        if metadata_obj:
            try:
                return True, "Metadata retrieved successfully.", json.dumps(metadata_obj, indent=4)
            except TypeError as e: 
                 return False, f"Error serializing metadata for task at '{task_directory}': {str(e)}", "{}"
        else:
            if f"Task directory {task_directory} not found" in error_msg:
                 return False, f"Task at directory '{task_directory}' not found.", "{}"
            return False, error_msg, "{}"