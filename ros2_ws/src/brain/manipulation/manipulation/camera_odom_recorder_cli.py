#!/usr/bin/env python3
"""
CLI for Camera + Odometry Recorder

Usage:
    ros2 run manipulation camera_odom_recorder_cli.py start
    ros2 run manipulation camera_odom_recorder_cli.py stop
    ros2 run manipulation camera_odom_recorder_cli.py status
"""

import sys
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger
import json


class RecorderCLI(Node):
    def __init__(self):
        super().__init__('camera_odom_recorder_cli')
        
        # Service clients
        self.start_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/start_recording'
        )
        self.stop_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/stop_recording'
        )
        self.status_client = self.create_client(
            Trigger, 'brain/camera_odom_recorder/get_status'
        )
    
    def wait_for_service(self, client, timeout=5.0):
        """Wait for a service to be available."""
        if not client.wait_for_service(timeout_sec=timeout):
            self.get_logger().error(
                f"Service {client.srv_name} not available. "
                "Is the camera_odom_recorder node running?"
            )
            return False
        return True
    
    def call_service(self, client, timeout=30.0):
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
    
    def start_recording(self):
        """Start recording."""
        print("Starting recording (calibrating + waiting for odom)...")
        response = self.call_service(self.start_client, timeout=30.0)  # Longer timeout for calibration
        if response:
            if response.success:
                print(f"✓ {response.message}")
            else:
                print(f"✗ Failed: {response.message}")
            return response.success
        return False
    
    def stop_recording(self):
        """Stop recording."""
        print("Stopping recording...")
        response = self.call_service(self.stop_client)
        if response:
            if response.success:
                print(f"✓ {response.message}")
            else:
                print(f"✗ Failed: {response.message}")
            return response.success
        return False
    
    def get_status(self):
        """Get recorder status."""
        response = self.call_service(self.status_client)
        if response and response.success:
            try:
                status = json.loads(response.message)
                print("\n=== Camera + Odom Recorder Status ===")
                print(f"  State:         {status.get('state', 'unknown')}")
                print(f"  Session:       {status.get('session_name', 'none')}")
                print(f"  Frame Count:   {status.get('frame_count', 0)}")
                print(f"  Streaming:     {status.get('streaming_mode', 'disk')}")
                print(f"  Topics Ready:  {status.get('all_topics_received', False)}")
                
                topics = status.get('topics_status', {})
                if topics:
                    print("\n  Topic Status:")
                    for topic, received in topics.items():
                        icon = "✓" if received else "✗"
                        print(f"    {icon} {topic}")
                print()
                return True
            except json.JSONDecodeError:
                print(f"Status: {response.message}")
                return True
        return False


def print_usage():
    """Print usage information."""
    print("""
Camera + Odometry Recorder CLI

Usage:
    camera_odom_recorder_cli.py <command>

Commands:
    start   - Start a new recording session
    stop    - Stop recording and save to disk
    status  - Get current recorder status
    help    - Show this help message

Examples:
    ros2 run manipulation camera_odom_recorder_cli.py start
    ros2 run manipulation camera_odom_recorder_cli.py status
    ros2 run manipulation camera_odom_recorder_cli.py stop
""")


def main(args=None):
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command in ['help', '-h', '--help']:
        print_usage()
        sys.exit(0)
    
    if command not in ['start', 'stop', 'status']:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)
    
    rclpy.init(args=args)
    cli = RecorderCLI()
    
    try:
        if command == 'start':
            success = cli.start_recording()
        elif command == 'stop':
            success = cli.stop_recording()
        elif command == 'status':
            success = cli.get_status()
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

