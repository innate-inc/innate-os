#!/usr/bin/env python3
"""
Example usage of Maurice robot controllers.

This example demonstrates how to use both the Dynamixel controller for the arm
and the WheelController for the wheels.
"""

import time
from maurice_control_simple.dynamixel import Dynamixel, OperatingMode, ReadAttribute
from maurice_control_simple.wheel_controller import WheelController


def main():
    """Main function to demonstrate the controllers."""
    print("Maurice Robot Control Example")
    print("-----------------------------")

    # Initialize the controllers
    try:
        # Initialize the wheel controller
        # Note: Adjust the port as needed for your system
        print("\nInitializing wheel controller...")
        wheel_ctrl = WheelController(
            port="/dev/ttyTHS1", debug=True  # Adjust as needed
        )

        # Initialize the Dynamixel controller for the arm
        # Note: Adjust the port as needed for your system
        print("\nInitializing Dynamixel controller for the arm...")
        arm_ctrl = Dynamixel(
            device_name="/dev/ttyACM0", baudrate=1000000  # Adjust as needed
        )

        # Example of controlling the wheels
        print("\nControlling the wheels:")
        print("1. Moving forward at 0.2 m/s")
        wheel_ctrl.set_speed(0.2, 0.0)  # Forward at 0.2 m/s
        time.sleep(2)

        print("2. Turning with angular velocity 0.5 rad/s")
        wheel_ctrl.set_speed(0.1, 0.5)  # Forward at 0.1 m/s with turning
        time.sleep(2)

        print("3. Stopping")
        wheel_ctrl.set_speed(0.0, 0.0)  # Stop
        time.sleep(1)

        # Request health information
        print("\nRequesting health information from wheels...")
        wheel_ctrl.request_status()
        time.sleep(0.5)  # Give time for the response to be processed

        print(f"Battery voltage: {wheel_ctrl.get_battery_voltage()} V")
        print(f"Motor temperature: {wheel_ctrl.get_motor_temperature()} °C")
        print(f"Fault code: {wheel_ctrl.get_fault_code()}")

        # Example of controlling the arm
        print("\nControlling the arm:")

        # Set operating mode for motor ID 1 to position control
        motor_id = 1  # Adjust as needed for your robot
        print(f"1. Setting motor {motor_id} to position control mode")
        arm_ctrl.set_operating_mode(motor_id, OperatingMode.POSITION)

        # Enable torque
        print(f"2. Enabling torque for motor {motor_id}")
        arm_ctrl._enable_torque(motor_id)

        # Move to position
        print(f"3. Moving motor {motor_id} to position 2000")
        arm_ctrl.set_position(motor_id, 2000)
        time.sleep(2)

        # Read current position
        position = arm_ctrl.read_attribute(motor_id, ReadAttribute.POSITION)
        print(f"4. Current position of motor {motor_id}: {position}")

        # Read temperature
        temp = arm_ctrl.read_attribute(motor_id, ReadAttribute.TEMPERATURE)
        print(f"5. Temperature of motor {motor_id}: {temp} °C")

        # Disable torque
        print(f"6. Disabling torque for motor {motor_id}")
        arm_ctrl._disable_torque(motor_id)

    except Exception as e:
        print(f"Error: {e}")

    finally:
        # Clean up
        print("\nCleaning up...")
        if "wheel_ctrl" in locals():
            del wheel_ctrl
        if "arm_ctrl" in locals():
            arm_ctrl.disconnect()

        print("\nExample completed.")


if __name__ == "__main__":
    main()
