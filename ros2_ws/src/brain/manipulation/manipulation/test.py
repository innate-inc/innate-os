#!/usr/bin/env python3
import h5py
import numpy as np
import matplotlib.pyplot as plt

# Path to the HDF5 file
path = '/home/vignesh/maurice-prod/data/Paper 4/episode_20.h5'

# Open the file and load the "action" dataset
with h5py.File(path, 'r') as f:
    actions = f['action'][:]  # Assuming actions has shape (timesteps, action_dimension)

# Extract the last two elements (columns) for all timesteps
last_two_actions = actions[:, -2:]

# Create a time axis assuming each row is a timestep
timesteps = np.arange(actions.shape[0])

# Plotting the two action elements
plt.figure(figsize=(10, 6))
plt.plot(timesteps, last_two_actions[:, 0], label='Action Element -2')
plt.plot(timesteps, last_two_actions[:, 1], label='Action Element -1')

plt.xlabel('Timestep')
plt.ylabel('Action Value')
plt.title('Time Series of the Last Two Action Elements')
plt.legend()
plt.grid(True)
plt.show()
