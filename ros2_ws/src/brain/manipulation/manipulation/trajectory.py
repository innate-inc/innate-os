#!/usr/bin/env python3
# trajectory.py
import numpy as np

def cubic_trajectory(start, end, total_time, freq=15):
    """
    Generate a cubic interpolated trajectory from start to end with zero initial and final speeds.

    Parameters:
        start (np.ndarray): Starting joint positions.
        end (np.ndarray): Ending joint positions.
        total_time (float): Total duration of the trajectory.
        num_steps (int): Number of steps in the generated time vector (default is 100).

    Returns:
        t (np.ndarray): Time vector from 0 to total_time.
        traj (np.ndarray): Trajectory positions for each joint at each time step.
                           Shape is (num_steps, number_of_joints).
    """
    # Create a time vector from 0 to total_time
    num_steps = int(total_time * freq)
    t = np.linspace(0, total_time, num_steps)
    
    # Normalize time to [0, 1]
    tau = t / total_time
    
    # Compute the cubic interpolation term
    # f(tau) = 3*tau^2 - 2*tau^3
    f_tau = 3 * tau**2 - 2 * tau**3
    
    # Calculate the trajectory for each joint
    # Using broadcasting: f_tau[:, None] makes it a column vector for proper multiplication
    traj = start + (end - start) * f_tau[:, None]
    
    return t, traj

# Example usage:
if __name__ == '__main__':
    start = np.array([0.0, 0.0, 0.0])
    end = np.array([1.0, 2.0, 3.0])
    total_time = 2.0  # seconds
    t, trajectory = cubic_trajectory(start, end, total_time, num_steps=50)
    
    print("Time vector:\n", t)
    print("Trajectory:\n", trajectory)
