#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import JointState
from maurice_msgs.srv import GotoJS
from std_msgs.msg import Float64MultiArray
import threading
import time
import math

class SquareMoveNode(Node):
    def __init__(self):
        super().__init__('square_move_node')

        # Publisher for IK delta commands
        self.delta_pub = self.create_publisher(Twist, 'ik_delta', 10)
        # Subscriber for IK solutions
        self.ik_solution_sub = self.create_subscription(
            JointState,
            'ik_solution',
            self.ik_solution_callback,
            10
        )
        # Service client for executing joint-space trajectories
        self.goto_js_client = self.create_client(GotoJS, 'maurice_arm/goto_js')

        # State
        self.latest_ik_solution = None
        self.ik_solution_received = False

        # Configuration for straight-line motion
        self.waypoint_frequency = 10.0  # Hz - frequency to process waypoints
        self.waypoint_spacing = 0.01    # meters - spacing between intermediate points
        self.waypoint_traj_time = 0.1   # seconds - time for each small trajectory segment

        self.get_logger().info('SquareMoveNode initialized')
        self.wait_for_services_and_pose()

    def wait_for_services_and_pose(self):
        # Wait for the trajectory service
        if not self.goto_js_client.wait_for_service(timeout_sec=10.0):
            self.get_logger().error('GotoJS service not available')
            rclpy.shutdown()
            return
        self.get_logger().info('GotoJS service available')

    def ik_solution_callback(self, msg: JointState):
        # Store the latest IK solution and flag
        self.latest_ik_solution = msg
        self.ik_solution_received = True
        self.get_logger().debug(f'Received IK solution: {msg.position}')

    def generate_straight_line_waypoints(self, start_pos, end_pos, spacing=None):
        """
        Generate intermediate waypoints along a straight line between start and end positions.
        
        Args:
            start_pos: tuple (x, y, z) starting position
            end_pos: tuple (x, y, z) ending position
            spacing: distance between waypoints (uses self.waypoint_spacing if None)
        
        Returns:
            List of (x, y, z) waypoints including start and end
        """
        if spacing is None:
            spacing = self.waypoint_spacing
            
        x1, y1, z1 = start_pos
        x2, y2, z2 = end_pos
        
        # Calculate total distance
        distance = math.sqrt((x2 - x1)**2 + (y2 - y1)**2 + (z2 - z1)**2)
        
        if distance < spacing:
            # If distance is very small, just return start and end
            return [start_pos, end_pos]
        
        # Calculate number of segments
        num_segments = max(1, int(distance / spacing))
        
        # Generate waypoints
        waypoints = []
        for i in range(num_segments + 1):
            t = i / num_segments
            x = x1 + t * (x2 - x1)
            y = y1 + t * (y2 - y1)
            z = z1 + t * (z2 - z1)
            waypoints.append((x, y, z))
        
        return waypoints

    def solve_and_execute_waypoint(self, x: float, y: float, z: float, traj_time: float = None):
        """
        Get IK solution for a single waypoint and execute it.
        
        Returns:
            bool: True if successful, False otherwise
        """
        if traj_time is None:
            traj_time = self.waypoint_traj_time
            
        # Prepare Twist message with absolute target pose
        twist = Twist()
        twist.linear.x = x
        twist.linear.y = y
        twist.linear.z = z
        twist.angular.x = 0.0
        twist.angular.y = 0.0
        twist.angular.z = 0.0

        # Publish IK request
        self.ik_solution_received = False
        self.delta_pub.publish(twist)
        self.get_logger().debug(f'Published target pose: x={x:.3f}, y={y:.3f}, z={z:.3f}')

        # Wait for IK solution
        start = time.time()
        while not self.ik_solution_received and (time.time() - start) < 2.0:
            rclpy.spin_once(self, timeout_sec=0.05)

        if not self.latest_ik_solution:
            self.get_logger().error(f'No IK solution received for waypoint ({x:.3f}, {y:.3f}, {z:.3f})')
            return False

        # Prepare service request
        req = GotoJS.Request()
        # Copy joint positions; append zero for missing joint if needed
        positions = list(self.latest_ik_solution.position)
        if len(positions) < 6:
            positions += [0.0] * (6 - len(positions))
        req.data = Float64MultiArray(data=positions)
        req.time = traj_time

        # Call the trajectory service
        future = self.goto_js_client.call_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
        res = future.result()
        
        if res and res.success:
            self.get_logger().debug(f'Moving to waypoint ({x:.3f}, {y:.3f}, {z:.3f}) with joints {positions}')
            # Wait for the trajectory to complete
            time.sleep(traj_time)
            return True
        else:
            self.get_logger().error(f'Trajectory execution failed for waypoint ({x:.3f}, {y:.3f}, {z:.3f})')
            return False

    def move_straight_line(self, start_pos, end_pos):
        """
        Move the end effector in a straight line from start_pos to end_pos.
        
        Args:
            start_pos: tuple (x, y, z) starting position
            end_pos: tuple (x, y, z) ending position
        """
        self.get_logger().info(f'Moving straight line from {start_pos} to {end_pos}')
        
        # Generate waypoints along the straight line
        waypoints = self.generate_straight_line_waypoints(start_pos, end_pos)
        self.get_logger().info(f'Generated {len(waypoints)} waypoints for straight line motion')
        
        # Process waypoints at the specified frequency
        waypoint_period = 1.0 / self.waypoint_frequency
        
        for i, (x, y, z) in enumerate(waypoints):
            start_time = time.time()
            
            # Execute this waypoint
            success = self.solve_and_execute_waypoint(x, y, z)
            if not success:
                self.get_logger().error(f'Failed to reach waypoint {i+1}/{len(waypoints)}, aborting straight line motion')
                return False
            
            # Maintain frequency by waiting if we finished early
            elapsed = time.time() - start_time
            if elapsed < waypoint_period:
                time.sleep(waypoint_period - elapsed)
        
        self.get_logger().info(f'Completed straight line motion to {end_pos}')
        return True

    def move_square(self):
        # Define square corners at z=0.15
        z = 0.15
        corners = [
            (0.2,  0.1),
            (0.3,  0.1),
            (0.3, -0.2),
            (0.2, -0.2),
            (0.2,  0.1),  # return to start
        ]
        
        # Convert to 3D positions
        corner_positions = [(x, y, z) for x, y in corners]
        
        # Move to first corner (from current position, assuming we start from a known pose)
        self.get_logger().info('Moving to first corner...')
        first_corner = corner_positions[0]
        success = self.solve_and_execute_waypoint(first_corner[0], first_corner[1], first_corner[2], traj_time=2.0)
        if not success:
            self.get_logger().error('Failed to reach first corner, aborting square motion')
            return
        
        # Move between corners in straight lines
        for i in range(1, len(corner_positions)):
            start_pos = corner_positions[i-1]
            end_pos = corner_positions[i]
            
            self.get_logger().info(f'Moving from corner {i} to corner {i+1}')
            success = self.move_straight_line(start_pos, end_pos)
            if not success:
                self.get_logger().error(f'Failed to complete straight line motion, aborting at corner {i+1}')
                return
            
            # Brief pause between segments
        
        self.get_logger().info('Completed square motion with straight line segments')


def main(args=None):
    rclpy.init(args=args)
    node = SquareMoveNode()

    # Spin in background to process IK callbacks
    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    # Allow time for initialization
    time.sleep(2.0)
    # Execute square trajectory
    node.move_square()

    # Shutdown
    node.get_logger().info('Shutting down')
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
