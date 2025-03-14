# Maurice Control Simple

A simple Python package to control Maurice robot without ROS.

## Installation

```bash
pip install -e .
```

## Usage

```python
from maurice_control_simple import MauriceController

# Initialize the controller
controller = MauriceController(device_name="/dev/ttyACM0")

# Move the robot forward
controller.move_forward(speed=0.5)  # Speed from 0.0 to 1.0

# Turn the robot
controller.turn(angle=0.5)  # Positive for left, negative for right, range -1.0 to 1.0

# Set arm joint positions
controller.set_arm_position([2048, 2048, 2048, 2048, 2048])  # Center positions

# Stop the robot
controller.stop()

# Close the connection
controller.close()
```

## Features

- Simple movement control (forward, backward, turning)
- Arm position control
- No ROS dependencies 