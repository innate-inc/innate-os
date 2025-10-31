#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS  # Service now includes an int32 time in the request

class ArmCommanderNode(Node):
    def __init__(self):
        super().__init__('arm_commander')

        # Subscriber to arm state. We store the latest state for trajectory planning.
        self.state_subscriber = self.create_subscription(
            JointState,
            '/mars/arm/state',
            self.state_callback,
            10
        )
        self.latest_state = None  # Latest joint positions (list of floats)

        # Publisher to arm commands
        self.command_publisher = self.create_publisher(
            Float64MultiArray,
            '/mars/arm/commands',
            10
        )

        # Service: accepts a target joint state and trajectory time, returns a success flag.
        self.goto_js_service = self.create_service(
            GotoJS,
            '/mars/arm/goto_js',
            self.goto_js_callback
        )

        # Timer for publishing trajectory commands at 30 Hz.
        self.trajectory_timer = self.create_timer(1.0 / 30.0, self.trajectory_timer_callback)

        # Variables to hold the computed trajectory
        self.trajectory_points = []  # List of joint state vectors
        self.trajectory_index = 0

        self.get_logger().info("Arm Commander Node has started.")

    def state_callback(self, msg: JointState):
        """Store the most recent joint state for trajectory planning."""
        if msg.position:
            self.latest_state = list(msg.position)
            self.get_logger().debug(f"Updated latest state: {self.latest_state}")
        else:
            self.get_logger().warn("Received JointState message with no position data.")

    def compute_trajectory(self, start, goal, T, dt=1.0/30.0):
        """
        Compute a cubic (third-order) spline trajectory between start and goal.
        Uses the formula:
            q(t) = q0 + (q1 - q0) * (3*(t/T)^2 - 2*(t/T)^3)
        which satisfies zero velocity at the start and end.
        """
        num_steps = int(T / dt) + 1
        trajectory = []
        for step in range(num_steps):
            t = step * dt
            ratio = 3 * (t / T) ** 2 - 2 * (t / T) ** 3
            point = [s + (g - s) * ratio for s, g in zip(start, goal)]
            trajectory.append(point)
        return trajectory

    def goto_js_callback(self, request, response):
        """
        Service callback for 'mars/arm/goto_js'.
        Expects:
          - request.data: a Float64MultiArray containing the target joint positions.
          - request.time: an int32 representing the total trajectory time in seconds.
        Computes a cubic trajectory from the current state to the target and starts publishing it.
        """
        # Extract the target joint positions and trajectory time from the service request.
        target = request.data.data
        total_time = float(request.time)

        # Validate trajectory time
        if total_time <= 0:
            self.get_logger().error("Trajectory time must be positive.")
            response.success = False
            return response

        # Check if we have a current state.
        if self.latest_state is None:
            error_msg = "No current joint state available. Cannot plan trajectory."
            self.get_logger().error(error_msg)
            response.success = False
            return response

        self.get_logger().info(f"Received goto_js service request. Target: {target}, Total time: {total_time}s")

        # Compute a cubic spline trajectory from the current state to the target.
        dt = 1.0 / 30.0  # 30 Hz update rate
        self.trajectory_points = self.compute_trajectory(self.latest_state, target, total_time, dt)
        self.trajectory_index = 0

        self.get_logger().info(f"Computed trajectory with {len(self.trajectory_points)} points.")
        response.success = True
        return response

    def trajectory_timer_callback(self):
        """Publish the next point in the trajectory at 30 Hz, if available."""
        if self.trajectory_index < len(self.trajectory_points):
            command_msg = Float64MultiArray()
            command_msg.data = self.trajectory_points[self.trajectory_index]
            self.command_publisher.publish(command_msg)
            self.get_logger().debug(f"Published trajectory point {self.trajectory_index}: {command_msg.data}")
            self.trajectory_index += 1

            if self.trajectory_index >= len(self.trajectory_points):
                self.get_logger().info("Completed publishing trajectory.")
                self.trajectory_points = []
                self.trajectory_index = 0

def main(args=None):
    rclpy.init(args=args)
    node = ArmCommanderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("Keyboard interrupt, shutting down node.")
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
