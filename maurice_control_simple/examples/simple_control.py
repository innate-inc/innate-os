#!/usr/bin/env python3
"""
Simple example script to demonstrate how to use the maurice_control_simple package.
"""

import time
import sys
import os

# Add the parent directory to the path so we can import the package
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from maurice_control_simple import MauriceController


def main():
    # Replace with your device name
    device_name = "/dev/ttyACM0"
    if len(sys.argv) > 1:
        device_name = sys.argv[1]

    print(f"Connecting to Maurice on {device_name}...")

    try:
        # Initialize the controller
        controller = MauriceController(device_name=device_name)

        # Demo sequence
        print("Moving forward...")
        controller.move_forward(speed=0.3)
        time.sleep(2)

        print("Stopping...")
        controller.stop()
        time.sleep(1)

        print("Turning left...")
        controller.turn(angle=0.3)
        time.sleep(2)

        print("Stopping...")
        controller.stop()
        time.sleep(1)

        print("Moving backward...")
        controller.move_backward(speed=0.3)
        time.sleep(2)

        print("Stopping...")
        controller.stop()
        time.sleep(1)

        print("Setting arm position...")
        # Set arm to center positions
        controller.set_arm_position([2048, 2048, 2048, 2048, 2048])
        time.sleep(2)

        # Read current arm position
        positions = controller.get_arm_position()
        print(f"Current arm positions: {positions}")

    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Make sure to close the connection
        if "controller" in locals():
            print("Closing connection...")
            controller.close()

    print("Demo completed")


if __name__ == "__main__":
    main()
