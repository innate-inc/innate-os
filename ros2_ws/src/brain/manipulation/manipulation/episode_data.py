import h5py
import numpy as np

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
