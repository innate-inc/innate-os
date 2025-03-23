# Maurice Control Simple

A simplified interface for controlling the Maurice robot without ROS.

## Overview

This package provides interfaces for controlling both the arm (using Dynamixel) and the wheels (using UART communication) of the Maurice robot.

- `Dynamixel` - Controls the robot arm using Dynamixel servos
- `WheelController` - Controls the robot wheels using UART communication

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd maurice-control-simple

# Install the package
pip install -e .
```

## Dependencies

- Python 3.6+
- dynamixel_sdk
- pyserial

You can install the dependencies with:

```bash
pip install dynamixel_sdk pyserial
```

## Usage

### Controlling the Arm

```python
from maurice_control_simple import Dynamixel, OperatingMode, ReadAttribute

# Initialize the controller
arm_ctrl = Dynamixel(
    device_name="/dev/ttyACM0",  # Adjust as needed
    baudrate=1000000
)

# Set operating mode for a motor
motor_id = 1
arm_ctrl.set_operating_mode(motor_id, OperatingMode.POSITION)

# Enable torque
arm_ctrl._enable_torque(motor_id)

# Move to position
arm_ctrl.set_position(motor_id, 2000)

# Read current position
position = arm_ctrl.read_attribute(motor_id, ReadAttribute.POSITION)
print(f"Current position: {position}")

# Read temperature
temp = arm_ctrl.read_attribute(motor_id, ReadAttribute.TEMPERATURE)
print(f"Temperature: {temp} °C")

# Disable torque when done
arm_ctrl._disable_torque(motor_id)

# Disconnect
arm_ctrl.disconnect()
```

### Controlling the Wheels

```python
from maurice_control_simple import WheelController
import time

# Initialize the controller
wheel_ctrl = WheelController(
    port="/dev/ttyTHS1",  # Adjust as needed
    debug=True
)

# Move forward at 0.2 m/s
wheel_ctrl.set_speed(0.2, 0.0)
time.sleep(2)

# Turn with angular velocity 0.5 rad/s
wheel_ctrl.set_speed(0.1, 0.5)
time.sleep(2)

# Stop
wheel_ctrl.set_speed(0.0, 0.0)

# Request health information
wheel_ctrl.request_status()
time.sleep(0.5)  # Give time for the response to be processed

# Get health information
print(f"Battery voltage: {wheel_ctrl.get_battery_voltage()} V")
print(f"Motor temperature: {wheel_ctrl.get_motor_temperature()} °C")
print(f"Fault code: {wheel_ctrl.get_fault_code()}")

# Get current position
x, y, theta = wheel_ctrl.get_position()
print(f"Position: x={x}, y={y}, theta={theta}")

# The controller will be cleaned up automatically when the object is destroyed
```

## Running Tests

The package includes simple tests to verify that the controllers send the correct signals. To run the tests:

```bash
# Navigate to the package directory
cd maurice_control_simple

# Run the tests with pytest
pytest
```

The tests use mocking to simulate the hardware interfaces, so they can be run without actual hardware connected. The tests verify:

1. For the Dynamixel controller (arm):
   - Setting operating modes
   - Enabling/disabling torque
   - Setting positions

2. For the WheelController (wheels):
   - Setting speed commands
   - Proper packet formatting and CRC calculation

## License

[MIT License](LICENSE) 