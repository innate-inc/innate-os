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
        
        # Separate timestamps for each data source (in seconds)
        self.arm_timestamps = []      # From arm_state.header.stamp
        self.image_timestamps = {}    # Dict: camera_name -> [timestamps]
        
        # Initialize images dict only if camera_names is provided
        if camera_names is not None:
            self.images = {cam: [] for cam in self.camera_names}
        else:
            self.images = {}
    
    def add_timestep(self, action, qpos, qvel, images, arm_timestamp=None, image_timestamps=None):
        """
        Add a new time step of data.
        
        On the first call, it dynamically sets the camera configuration based on the provided images.
        Subsequent calls must match the initial camera configuration.
        
        Args:
            action: Data representing the action at the current time step.
            qpos: Data representing the robot's position at the current time step.
            qvel: Data representing the robot's velocity at the current time step.
            images (list): A list of images.
            arm_timestamp (float, optional): Timestamp in seconds from arm_state message.
            image_timestamps (list, optional): List of timestamps for each image (same order as images).
        
        Raises:
            ValueError: If a subsequent call does not provide the same number of images as initially determined.
        """
        if self.camera_names is None:
            # Dynamically set camera names based on the number of images in the first timestep
            self.camera_names = [f"camera_{i+1}" for i in range(len(images))]
            self.images = {cam: [] for cam in self.camera_names}
            self.image_timestamps = {cam: [] for cam in self.camera_names}
        elif len(images) != len(self.camera_names):
            raise ValueError(f"Expected {len(self.camera_names)} images, but got {len(images)}")
        
        self.actions.append(action)
        self.qpos.append(qpos)
        self.qvel.append(qvel)
        
        # Store arm timestamp
        if arm_timestamp is not None:
            self.arm_timestamps.append(arm_timestamp)
        
        # Store image timestamps
        if image_timestamps is not None:
            for idx, cam in enumerate(self.camera_names):
                if idx < len(image_timestamps):
                    self.image_timestamps[cam].append(image_timestamps[idx])
        
        # Append each image to the appropriate camera's list
        for idx, cam in enumerate(self.camera_names):
            self.images[cam].append(images[idx])
    
    def add_termination_data(self):
        """
        Add termination data to the actions. This method adds two additional dimensions
        to each action timestep:
        - First dimension: Linear progression from 0 to 1 across all timesteps
        - Second dimension: 0 for all timesteps except the last 10, where it's 1
        """
        if not self.actions:
            return  # No actions to modify
        
        total_timesteps = len(self.actions)
        
        # Convert actions to numpy array
        actions_array = np.array(self.actions)
        
        # Create first dimension: linear progression from 0 to 1
        linear_values = np.linspace(0, 1, total_timesteps) if total_timesteps > 1 else np.array([0.0])
        
        # Create second dimension: 0 for all except last 10 timesteps
        termination_values = np.zeros(total_timesteps)
        if total_timesteps >= 10:
            termination_values[-10:] = 1.0
        else:
            termination_values[:] = 1.0  # If less than 10 timesteps, all get 1.0
        
        # Stack the additional dimensions and concatenate with actions
        additional_dims = np.column_stack([linear_values, termination_values])
        extended_actions = np.concatenate([actions_array, additional_dims], axis=1)
        
        # Convert back to list format
        self.actions = extended_actions.tolist()
    
    def save_file(self, path):
        """
        Save the buffered episode data into an HDF5 file at the specified path.
        
        The file will have the following structure:
        
            /action         -> Dataset containing the action data.
            /timestamps
                ├── arm       -> Timestamps from arm_state messages
                └── images
                      ├── camera_1 -> Timestamps for camera 1
                      └── camera_2 -> Timestamps for camera 2
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
        # Automatically add termination data before saving
        self.add_termination_data()
        
        with h5py.File(path, 'w') as hf:
            # Always save actions
            hf.create_dataset('/action', data=np.array(self.actions))
            
            # Save timestamps if available
            has_arm_ts = len(self.arm_timestamps) > 0
            has_img_ts = any(len(ts) > 0 for ts in self.image_timestamps.values())
            
            if has_arm_ts or has_img_ts:
                ts_group = hf.create_group('/timestamps')
                if has_arm_ts:
                    ts_group.create_dataset('arm', data=np.array(self.arm_timestamps))
                if has_img_ts:
                    img_ts_group = ts_group.create_group('images')
                    for cam_name, ts_list in self.image_timestamps.items():
                        if len(ts_list) > 0:
                            img_ts_group.create_dataset(cam_name, data=np.array(ts_list))

            # Detect available observations
            has_qpos = isinstance(self.qpos, list) and len(self.qpos) > 0 and any(len(x) > 0 for x in self.qpos)
            has_qvel = isinstance(self.qvel, list) and len(self.qvel) > 0 and any(len(x) > 0 for x in self.qvel)
            has_images = isinstance(self.images, dict) and len(self.images) > 0 and any(len(v) > 0 for v in self.images.values())

            # Only create observations if any are present
            if has_qpos or has_qvel or has_images:
                obs_group = hf.create_group('/observations')
                if has_qpos:
                    obs_group.create_dataset('qpos', data=np.array(self.qpos))
                if has_qvel:
                    obs_group.create_dataset('qvel', data=np.array(self.qvel))
                if has_images:
                    images_group = obs_group.create_group('images')
                    for cam_name, image_list in self.images.items():
                        if len(image_list) == 0:
                            continue
                        images_group.create_dataset(cam_name, data=np.array(image_list))
    
    def clear(self):
        """
        Clear all buffered data.
        
        This method resets the buffers for actions, qpos, qvel, and images,
        maintaining the same camera configuration as before.
        """
        self.actions = []
        self.qpos = []
        self.qvel = []
        self.arm_timestamps = []
        self.image_timestamps = {cam: [] for cam in self.camera_names} if self.camera_names else {}
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
            'arm_timestamps': self.arm_timestamps,
            'image_timestamps': self.image_timestamps,
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

    def start_new_task(self, task_name, data_frequency, primitive_type='learned'):
        """
        Start a new task by creating the data directory and initializing dataset metadata.
        If dataset_metadata.json already exists, the task will be resumed instead.
        Also creates the primitive metadata.json file for the primitive server.

        Args:
            task_name (str): The name for the new task (primitive name).
            data_frequency (float): The frequency at which data is collected (in Hz).
            primitive_type (str): The type of primitive being created (default: 'learned').
        """
        self.current_task_name = task_name
        self.current_task_dir = os.path.join(self.base_data_directory, task_name)
        data_dir = os.path.join(self.current_task_dir, "data")
        dataset_metadata_path = os.path.join(data_dir, "dataset_metadata.json")

        if os.path.exists(dataset_metadata_path):
            # Dataset already exists; resume it.
            print(f"Dataset for '{task_name}' already exists. Resuming.")
            self.resume_task(task_name)
            return

        # Create data directory and initialize dataset metadata.
        os.makedirs(data_dir, exist_ok=True)
        self.metadata = {
            "data_frequency": data_frequency,
            "number_of_episodes": 0,
            "episodes": []  # Will contain details for each saved episode.
        }
        self._save_metadata()
        self.episodes = []  # Reset the episodes list.
        
        # Create the primitive metadata.json file for the primitive server
        self._create_primitive_metadata(task_name, primitive_type)

    def resume_task(self, task_name):
        """
        Resume a previously started task by loading its dataset metadata.
        
        Args:
            task_name (str): The name of the task to resume.
        
        Raises:
            FileNotFoundError: If the data directory or dataset_metadata.json does not exist.
        """
        self.current_task_name = task_name
        self.current_task_dir = os.path.join(self.base_data_directory, task_name)
        data_dir = os.path.join(self.current_task_dir, "data")
        metadata_path = os.path.join(data_dir, "dataset_metadata.json")
        if not os.path.exists(metadata_path):
            raise FileNotFoundError(f"Dataset metadata not found for task '{task_name}' at {metadata_path}")
        self.load_metadata()
        # Optionally, the episodes list can be updated if needed by reading from the HDF5 files.
        # For now, we leave self.episodes as an empty list.
    
    def add_episode(self, episode_data, start_timestamp, end_timestamp):
        """
        Save an episode's data to an HDF5 file and update the task's metadata.
        Episodes are stored in the 'data/' subdirectory of the task.

        Args:
            episode_data (EpisodeData): The EpisodeData object containing buffered data.
            start_timestamp (str): Start timestamp of the episode (e.g., ISO format).
            end_timestamp (str): End timestamp of the episode.
        """
        # Ensure data subdirectory exists
        data_dir = os.path.join(self.current_task_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        
        # Determine new episode ID and filename.
        episode_id = self.metadata["number_of_episodes"]
        file_name = f"episode_{episode_id}.h5"
        file_path = os.path.join(data_dir, file_name)
        
        # Save the episode HDF5 file.
        episode_data.save_file(file_path)
        
        # Update metadata with new episode info.
        episode_info = {
            "episode_id": episode_id,
            "file_name": file_name,  # Just the filename since metadata is in data/ directory
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
        Save the current metadata to dataset_metadata.json in the data directory.
        """
        if self.current_task_dir is None:
            raise RuntimeError("No active task directory to save metadata.")
        data_dir = os.path.join(self.current_task_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        metadata_path = os.path.join(data_dir, "dataset_metadata.json")
        with open(metadata_path, 'w') as f:
            json.dump(self.metadata, f, indent=4)

    def _create_primitive_metadata(self, task_name, primitive_type):
        """
        Create the metadata.json file for the primitive server.
        This file is placed in the root of the primitive directory (not in data/).
        
        Args:
            task_name (str): The name of the primitive.
            primitive_type (str): The type of primitive ('learned', 'replay', etc.).
        """
        if self.current_task_dir is None:
            raise RuntimeError("No active task directory to create primitive metadata.")
        
        primitive_metadata_path = os.path.join(self.current_task_dir, "metadata.json")
        
        # Only create if it doesn't already exist
        if os.path.exists(primitive_metadata_path):
            print(f"Primitive metadata.json already exists for '{task_name}'. Skipping creation.")
            return
        
        primitive_metadata = {
            "name": task_name,
            "type": primitive_type,
            "guidelines": "",
            "guidelines_when_running": "",
            "inputs": {},
            "execution": {
                # Checkpoint will be filled in after training
                # "checkpoint": "best_model.pt",
                # "stats_file": "dataset_stats.pt"
            }
        }
        
        with open(primitive_metadata_path, 'w') as f:
            json.dump(primitive_metadata, f, indent=4)
        
        print(f"Created primitive metadata.json for '{task_name}' (type: {primitive_type})")

    def load_metadata(self):
        """
        Load the metadata from dataset_metadata.json in the data directory.
        """
        if self.current_task_dir is None:
            raise RuntimeError("No active task directory to load metadata from.")
        metadata_path = os.path.join(self.current_task_dir, "data", "dataset_metadata.json")
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
        Loads, enriches (with episode num_timesteps), and returns dataset metadata for a single task
        using its absolute directory path.

        Args:
            task_directory (str): Absolute path to the task directory.

        Returns:
            tuple: (dict_or_None, error_message_or_None)
                   - dict_or_None: The enriched dataset metadata.
                   - error_message_or_None: Description of error if any.
        """
        data_dir = os.path.join(task_directory, "data")
        metadata_file_path = os.path.join(data_dir, "dataset_metadata.json")
        task_name = os.path.basename(task_directory)

        if not os.path.exists(task_directory) or not os.path.isdir(task_directory):
            return None, f"Task directory {task_directory} not found or is not a directory."
        
        if not os.path.exists(metadata_file_path):
            return None, f"dataset_metadata.json not found in {data_dir}."

        try:
            with open(metadata_file_path, 'r') as f:
                dataset_metadata = json.load(f)
        except json.JSONDecodeError as e:
            return None, f"Error decoding dataset_metadata.json in {data_dir}: {e}"
        except Exception as e:
            return None, f"Error reading dataset_metadata.json in {data_dir}: {e}"

        processed_episodes = []
        if "episodes" in dataset_metadata and isinstance(dataset_metadata["episodes"], list):
            for episode_info in dataset_metadata["episodes"]:
                num_timesteps = 0
                episode_file_name = episode_info.get("file_name", "")
                # Episodes are in data/ directory alongside dataset_metadata.json
                episode_file_path = os.path.join(data_dir, episode_file_name)
                
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
                    "file_name": episode_file_name
                })
        
        enriched_metadata = {
            "task_name": task_name,
            "task_directory": task_directory,
            "data_frequency": dataset_metadata.get("data_frequency", 0),
            "number_of_episodes": dataset_metadata.get("number_of_episodes", 0),
            "episodes": processed_episodes
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