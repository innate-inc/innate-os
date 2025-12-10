#!/usr/bin/env python3
"""
CLI for Whiteboard Drawing

Usage:
    ros2 run manipulation whiteboard_draw_cli.py record_corner
    ros2 run manipulation whiteboard_draw_cli.py start_drawing
"""

import sys
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger


class WhiteboardDrawCLI(Node):
    def __init__(self):
        super().__init__('whiteboard_draw_cli')
        
        # Service clients
        self.record_corner_client = self.create_client(
            Trigger, '/whiteboard_draw/record_corner'
        )
        self.start_drawing_client = self.create_client(
            Trigger, '/whiteboard_draw/start_drawing'
        )
    
    def wait_for_service(self, client, timeout=5.0):
        """Wait for a service to be available."""
        if not client.wait_for_service(timeout_sec=timeout):
            self.get_logger().error(
                f"Service {client.srv_name} not available. "
                "Is the whiteboard_draw node running?"
            )
            return False
        return True
    
    def call_service(self, client, timeout=10.0):
        """Call a Trigger service and return the response."""
        if not self.wait_for_service(client):
            return None
        
        request = Trigger.Request()
        future = client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout)
        
        if future.result() is None:
            self.get_logger().error("Service call failed or timed out.")
            return None
        
        return future.result()
    
    def record_corner(self):
        """Record current corner position."""
        print("Recording current corner position...")
        response = self.call_service(self.record_corner_client)
        if response:
            if response.success:
                print(f"✓ {response.message}")
            else:
                print(f"✗ Failed: {response.message}")
            return response.success
        return False
    
    def start_drawing(self):
        """Start drawing sequence."""
        print("Starting drawing sequence...")
        response = self.call_service(self.start_drawing_client)
        if response:
            if response.success:
                print(f"✓ {response.message}")
            else:
                print(f"✗ Failed: {response.message}")
            return response.success
        return False


def print_usage():
    """Print usage information."""
    print("""
Whiteboard Drawing CLI

Usage:
    whiteboard_draw_cli.py <command>

Commands:
    record_corner  - Record the current arm position as a calibration corner
    start_drawing  - Start the drawing sequence (after calibration)

Examples:
    # Calibration sequence:
    ros2 run manipulation whiteboard_draw_cli.py record_corner  # top-left
    ros2 run manipulation whiteboard_draw_cli.py record_corner  # top-right
    ros2 run manipulation whiteboard_draw_cli.py record_corner  # bottom-right
    ros2 run manipulation whiteboard_draw_cli.py record_corner  # bottom-left
    
    # Start drawing:
    ros2 run manipulation whiteboard_draw_cli.py start_drawing
""")


def main(args=None):
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command in ['help', '-h', '--help']:
        print_usage()
        sys.exit(0)
    
    if command not in ['record_corner', 'start_drawing']:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)
    
    rclpy.init(args=args)
    cli = WhiteboardDrawCLI()
    
    try:
        if command == 'record_corner':
            success = cli.record_corner()
        elif command == 'start_drawing':
            success = cli.start_drawing()
        else:
            success = False
        
        sys.exit(0 if success else 1)
    
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
    
    finally:
        cli.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

