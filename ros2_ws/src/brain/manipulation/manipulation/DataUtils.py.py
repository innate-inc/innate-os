import h5py
import numpy as np
import os
import json

class EpisodeData:
    def __init__(self, camera_names=None):
        """
        Initialize the EpisodeData instance.
        
        Args:
            camera_names (list, optional): List of camera names.
                If not provided, defaults to ['camera1', 'camera2'].
        """
        if camera_names is None:
            camera_names = ['camera1', 'camera2']
        self.camera_names = camera_names
        
        # Initialize buffers for time-step data.
        self.actions = []
        self.qpos = []
        self.qvel = []
        
        # Create a dictionary for images with fixed camera names.
        self.images = {cam: [] for cam in self.camera_names}
    
    def add_timestep(self, action, qpos, qvel, images):
        """
        Add a new time step of data.
        
        The provided 'images' list must be ordered such that the first element corresponds
        to 'camera1', the second to 'camera2', and so on.
        
        Args:
            action: Data representing the action at the current time step.
            qpos: Data representing the robot's position at the current time step.
            qvel: Data representing the robot's velocity at the current time step.
            images (list): A list of images corresponding to the default camera ordering.
        
        Raises:
            ValueError: If the number of images does not match the number of cameras.
        """
        if len(images) != len(self.camera_names):
            raise ValueError(f"Expected {len(self.camera_names)} images, but got {len(images)}")
        
        self.actions.append(action)
        self.qpos.append(qpos)
        self.qvel.append(qvel)
        
        # Append each image to the appropriate camera's list.
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
        
        This method resets the buffers for actions, qpos, qvel, and images.
        """
        self.actions = []
        self.qpos = []
        self.qvel = []
        self.images = {cam: [] for cam in self.camera_names}
    
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

    def start_new_task(self, task_name, task_description, mobile_flag):
        """
        Start a new task by creating a task directory and initializing metadata.

        Args:
            task_name (str): The name for the new task.
            task_description (str): A description for the task.
            mobile_flag (bool): Indicates if the task involves mobile data.
        """
        self.current_task_name = task_name
        self.current_task_dir = os.path.join(self.base_data_directory, task_name)
        os.makedirs(self.current_task_dir, exist_ok=True)
        
        # Initialize metadata dictionary.
        self.metadata = {
            "task_name": task_name,
            "task_description": task_description,
            "mobile_task": mobile_flag,
            "number_of_episodes": 0,
            "episodes": []  # Will contain details for each saved episode.
        }
        self._save_metadata()
        self.episodes = []  # Reset the episodes list.

    def add_episode(self, episode_data, start_timestamp, end_timestamp):
        """
        Save an episode's data to an HDF5 file and update the task's metadata.

        Args:
            episode_data (EpisodeData): The EpisodeData object containing buffered data.
            start_timestamp (str): Start timestamp of the episode (e.g., ISO format).
            end_timestamp (str): End timestamp of the episode.
        """
        # Determine new episode ID and filename.
        episode_id = self.metadata["number_of_episodes"] + 1
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
        self.episodes.append(episode_data)

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