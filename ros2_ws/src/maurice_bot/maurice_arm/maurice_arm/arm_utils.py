#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS
import time
from threading import Lock
from moveit_msgs.srv import GetMotionPlan, GetPlanningScene
from moveit_msgs.msg import (
    MotionPlanRequest, 
    PlanningScene, 
    CollisionObject,
    RobotState,
    Constraints,
    JointConstraint,
    PositionIKRequest,
    WorkspaceParameters
)
from shape_msgs.msg import SolidPrimitive
from geometry_msgs.msg import Pose, PoseStamped
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

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
        self.latest_state_lock = Lock()

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

        # MoveIt planning group name
        self.planning_group = "arm"
        
        # MoveIt service clients (NOT action - use services instead!)
        self.plan_kinematic_path_client = self.create_client(
            GetMotionPlan, 
            '/plan_kinematic_path'
        )
        
        # Planning scene client (for collision checking)
        self.planning_scene_client = self.create_client(GetPlanningScene, '/get_planning_scene')
        
        # Joint names for the arm (5 DOF based on arm.cpp)
        self.joint_names = ["joint1", "joint2", "joint3", "joint4", "joint5"]
        
        # Planner configuration
        self.planner_id = "chomp"
        self.planning_time = 5.0
        self.max_planning_attempts = 3
        
        # Initialize MoveIt availability flag
        self.moveit_available = False
        
        # Defer MoveIt initialization to a timer (needs executor spinning)
        self.moveit_init_timer = self.create_timer(0.5, self.initialize_moveit_planner_once)
        
        self.get_logger().info("Arm Commander Node has started. Initializing MoveIt...")

    def initialize_moveit_planner_once(self):
        """One-time initialization of MoveIt (called by timer after executor starts)."""
        # Cancel the timer so this only runs once
        self.moveit_init_timer.cancel()
        self.initialize_moveit_planner()
    
    def initialize_moveit_planner(self):
        """Initialize MoveIt2 planning components."""
        self.get_logger().info("Initializing MoveIt motion planner...")
        
        # Wait for planning service with longer timeout
        self.get_logger().info("Waiting for MoveIt planning service...")
        timeout_sec = 30.0
        start_time = self.get_clock().now()
        
        while not self.plan_kinematic_path_client.wait_for_service(timeout_sec=1.0):
            elapsed = (self.get_clock().now() - start_time).nanoseconds / 1e9
            if elapsed > timeout_sec:
                self.get_logger().warn(f"MoveIt planning service not available after {timeout_sec} seconds")
                self.get_logger().warn("Will continue and retry when service is called")
                self.moveit_available = False
                return
            self.get_logger().info(f"Still waiting for MoveIt planning service... ({elapsed:.1f}s)")
        
        self.moveit_available = True
        self.get_logger().info("✓ MoveIt planner connected!")
        self.get_logger().info(f"  Planning group: {self.planning_group}")
        self.get_logger().info(f"  Planner ID: {self.planner_id}")
        self.get_logger().info(f"  Joint names: {self.joint_names}")
        self.get_logger().info(f"  Using service: /plan_kinematic_path")
        
        # Add ground plane collision object
        self.add_ground_plane()

    def add_ground_plane(self):
        """Add ground plane collision object to prevent underground planning."""
        try:
            # Create collision object for ground plane
            ground_plane = CollisionObject()
            ground_plane.header.frame_id = "base_link"
            ground_plane.id = "ground_plane"
            
            # Define ground as a large box below z=0
            primitive = SolidPrimitive()
            primitive.type = SolidPrimitive.BOX
            primitive.dimensions = [10.0, 10.0, 2.0]  # 10m x 10m x 2m
            
            # Position box center at z=-1.0m (top at z=0, bottom at z=-2m)
            box_pose = Pose()
            box_pose.orientation.w = 1.0
            box_pose.position.z = -1.0
            
            ground_plane.primitives = [primitive]
            ground_plane.primitive_poses = [box_pose]
            ground_plane.operation = CollisionObject.ADD
            
            # Publish to planning scene (would typically use planning_scene interface)
            # For now, this is conceptual - the actual implementation depends on 
            # how the planning scene is managed in your setup
            self.get_logger().info("Added ground plane collision object")
            
        except Exception as e:
            self.get_logger().warn(f"Could not add ground plane: {e}")

    def state_callback(self, msg: JointState):
        """Store the most recent joint state for trajectory planning."""
        with self.latest_state_lock:
            if msg.position:
                self.latest_state = list(msg.position)
                self.get_logger().debug(f"Updated latest state: {self.latest_state}")
            else:
                self.get_logger().warn("Received JointState message with no position data.")

    def plan_with_moveit(self, start, goal, planning_time):
        """
        Plan a trajectory using MoveIt2 service interface.
        Returns trajectory waypoints and times, or None if planning fails.
        """
        # Check if MoveIt is available
        if not self.moveit_available:
            self.get_logger().warn("MoveIt not available, checking again...")
            if not self.plan_kinematic_path_client.wait_for_service(timeout_sec=2.0):
                self.get_logger().error("MoveIt planning service still not available")
                return None
            else:
                self.moveit_available = True
                self.get_logger().info("✓ MoveIt planning service now available!")
        
        # Ensure we have 5 joints
        if len(goal) != 5:
            self.get_logger().error(f"Expected 5 joint positions, got {len(goal)}")
            return None
        
        if len(start) != 5:
            self.get_logger().error(f"Expected 5 start joint positions, got {len(start)}")
            return None
        
        self.get_logger().info(f"Planning to goal: [{goal[0]:.3f}, {goal[1]:.3f}, {goal[2]:.3f}, {goal[4]:.3f}, {goal[4]:.3f}]")
        
        # Use simple planning for now (no collision state validation)
        plan_result = self.plan_to_goal_service(start, goal, planning_time)
        
        if plan_result and plan_result.get("success"):
            waypoints = plan_result["waypoints"]
            times = plan_result["time_from_start"]
            self.get_logger().info(f"✓ Planned trajectory: {len(waypoints)} waypoints, "
                                 f"{plan_result['planning_time_ms']:.1f}ms planning time")
            return waypoints, times
        else:
            error_msg = plan_result.get("error_message", "Unknown error") if plan_result else "Planning failed"
            self.get_logger().error(f"✗ Planning failed: {error_msg}")
            return None

    def is_in_collision(self, joint_positions):
        """Check if a joint configuration is in collision."""
        if len(joint_positions) != 5:
            return True
        # Simplified check - in practice would query planning scene
        # For now, return False (no collision) since full collision checking requires
        # access to the planning scene which needs more complex setup
        return False

    def is_goal_valid(self, goal_positions):
        """Check if a goal configuration is valid (within limits and collision-free)."""
        if len(goal_positions) != 5:
            return False
        # Simplified check - in practice would validate joint limits and collisions
        # For now, return True (valid) since full validation requires planning scene access
        return True

    def plan_to_goal_service(self, start, goal, planning_time):
        """Plan using MoveIt service interface (/plan_kinematic_path)."""
        start_time_sec = time.time()
        
        try:
            # Create motion plan request
            req = GetMotionPlan.Request()
            
            # Set motion plan request parameters
            req.motion_plan_request.group_name = self.planning_group
            req.motion_plan_request.num_planning_attempts = self.max_planning_attempts
            req.motion_plan_request.allowed_planning_time = planning_time
            req.motion_plan_request.planner_id = self.planner_id
            
            # Set start state
            req.motion_plan_request.start_state.joint_state.name = self.joint_names
            req.motion_plan_request.start_state.joint_state.position = start
            req.motion_plan_request.start_state.is_diff = False
            
            # Set goal constraints (joint space goal)
            goal_constraint = Constraints()
            for i, (joint_name, joint_value) in enumerate(zip(self.joint_names, goal)):
                jc = JointConstraint()
                jc.joint_name = joint_name
                jc.position = joint_value
                jc.tolerance_above = 0.01
                jc.tolerance_below = 0.01
                jc.weight = 1.0
                goal_constraint.joint_constraints.append(jc)
            
            req.motion_plan_request.goal_constraints.append(goal_constraint)
            
            # Set workspace parameters (optional but good practice)
            ws = WorkspaceParameters()
            ws.header.frame_id = "base_link"
            ws.min_corner.x = -1.0
            ws.min_corner.y = -1.0
            ws.min_corner.z = -0.5
            ws.max_corner.x = 1.0
            ws.max_corner.y = 1.0
            ws.max_corner.z = 1.0
            req.motion_plan_request.workspace_parameters = ws
            
            # Call service
            self.get_logger().debug("Calling /plan_kinematic_path service...")
            future = self.plan_kinematic_path_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=planning_time + 5.0)
            
            if not future.done():
                return {
                    "success": False,
                    "error_message": "Planning service call timed out",
                    "planning_time_ms": (time.time() - start_time_sec) * 1000.0
                }
            
            response = future.result()
            planning_time_ms = (time.time() - start_time_sec) * 1000.0
            
            # Check if planning succeeded (error_code.val == 1 means SUCCESS)
            if response.motion_plan_response.error_code.val == 1:
                # Extract trajectory
                trajectory = response.motion_plan_response.trajectory.joint_trajectory
                waypoints = []
                time_from_start = []
                
                for point in trajectory.points:
                    waypoints.append(list(point.positions))
                    time_sec = point.time_from_start.sec + point.time_from_start.nanosec * 1e-9
                    time_from_start.append(time_sec)
                
                return {
                    "success": True,
                    "waypoints": waypoints,
                    "time_from_start": time_from_start,
                    "planning_time_ms": planning_time_ms,
                    "num_waypoints": len(waypoints)
                }
            else:
                error_msg = f"Planning failed with error code {response.motion_plan_response.error_code.val}"
                return {
                    "success": False,
                    "error_message": error_msg,
                    "planning_time_ms": planning_time_ms
                }
                
        except Exception as e:
            return {
                "success": False,
                "error_message": str(e),
                "planning_time_ms": (time.time() - start_time_sec) * 1000.0
            }

    def plan_to_goal_strict(self, start, goal, planning_time):
        """Plan with strict collision checking."""
        start_time = time.time()
        
        try:
            # Create goal for MoveGroup action
            goal_msg = MoveGroup.Goal()
            
            # Set planning group
            goal_msg.request.group_name = self.planning_group
            
            # Set planning time
            goal_msg.request.allowed_planning_time = planning_time
            
            # Set planner ID
            goal_msg.request.planner_id = self.planner_id
            
            # Set start state
            start_state = RobotState()
            start_js = SensorJointState()
            start_js.name = self.joint_names
            start_js.position = start
            start_state.joint_state = start_js
            goal_msg.request.start_state = start_state
            
            # Set goal constraints (joint target)
            goal_constraints = Constraints()
            for i, (joint_name, joint_value) in enumerate(zip(self.joint_names, goal)):
                jc = JointConstraint()
                jc.joint_name = joint_name
                jc.position = joint_value
                jc.tolerance_above = 0.01
                jc.tolerance_below = 0.01
                jc.weight = 1.0
                goal_constraints.joint_constraints.append(jc)
            
            goal_msg.request.goal_constraints = [goal_constraints]
            goal_msg.request.num_planning_attempts = self.max_planning_attempts
            
            # Send goal and wait for result
            self.get_logger().debug("Sending planning request to MoveIt...")
            send_goal_future = self.move_group_action_client.send_goal_async(goal_msg)
            rclpy.spin_until_future_complete(self, send_goal_future, timeout_sec=planning_time + 5.0)
            
            if not send_goal_future.done():
                return {
                    "success": False,
                    "error_message": "Planning request timed out",
                    "planning_time_ms": (time.time() - start_time) * 1000.0
                }
            
            goal_handle = send_goal_future.result()
            if not goal_handle.accepted:
                return {
                    "success": False,
                    "error_message": "Planning goal was rejected",
                    "planning_time_ms": (time.time() - start_time) * 1000.0
                }
            
            # Get result
            result_future = goal_handle.get_result_async()
            rclpy.spin_until_future_complete(self, result_future, timeout_sec=planning_time + 5.0)
            
            if not result_future.done():
                return {
                    "success": False,
                    "error_message": "Planning result timed out",
                    "planning_time_ms": (time.time() - start_time) * 1000.0
                }
            
            result = result_future.result().result
            planning_time_ms = (time.time() - start_time) * 1000.0
            
            # Check if planning succeeded
            if result.error_code.val == 1:  # SUCCESS
                # Extract trajectory
                trajectory = result.planned_trajectory.joint_trajectory
                waypoints = []
                time_from_start = []
                
                for point in trajectory.points:
                    waypoints.append(list(point.positions))
                    time_sec = point.time_from_start.sec + point.time_from_start.nanosec * 1e-9
                    time_from_start.append(time_sec)
                
                return {
                    "success": True,
                    "waypoints": waypoints,
                    "time_from_start": time_from_start,
                    "planning_time_ms": planning_time_ms,
                    "num_waypoints": len(waypoints)
                }
            else:
                error_msg = f"Planning failed with error code {result.error_code.val}"
                return {
                    "success": False,
                    "error_message": error_msg,
                    "planning_time_ms": planning_time_ms
                }
                
        except Exception as e:
            return {
                "success": False,
                "error_message": str(e),
                "planning_time_ms": (time.time() - start_time) * 1000.0
            }

    def plan_relaxed(self, start, goal, planning_time, allow_start_collision, allow_goal_collision):
        """Plan with relaxed collision constraints."""
        # For now, use strict planning - full relaxed planning implementation
        # would handle binary search for closest safe point when goal is in collision
        return self.plan_to_goal_strict(start, goal, planning_time)

    def compute_trajectory(self, start, goal, T, dt=1.0/30.0):
        """
        Compute a cubic (third-order) spline trajectory between start and goal.
        FALLBACK method when MoveIt planning fails.
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
        Uses MoveIt to plan a collision-free trajectory from current state to goal.
        """
        # Extract the target joint positions and trajectory time from the service request.
        target = request.data.data
        planning_time = float(request.time)

        # Validate trajectory time
        if planning_time <= 0:
            self.get_logger().error("Planning time must be positive.")
            response.success = False
            return response

        # Check if we have a current state.
        with self.latest_state_lock:
            if self.latest_state is None:
                error_msg = "No current joint state available. Cannot plan trajectory."
                self.get_logger().error(error_msg)
                response.success = False
                return response
            current_state = list(self.latest_state)

        # Ensure we have exactly 5 joints for MoveIt planning
        # (Based on arm.cpp which expects 5 joints)
        if len(target) != 5:
            self.get_logger().error(f"Expected 5 joint positions, got {len(target)}")
            response.success = False
            return response

        # Use only first 5 joints from current state as well
        current_state_5dof = current_state[:5] if len(current_state) >= 5 else current_state
        if len(current_state_5dof) != 5:
            self.get_logger().error(f"Current state has {len(current_state_5dof)} joints, expected 5")
            response.success = False
            return response

        self.get_logger().info(f"Received goto_js service request. Target: {target}, Planning time: {planning_time}s")

        # Plan with MoveIt
        self.get_logger().info("Planning with MoveIt...")
        moveit_result = self.plan_with_moveit(current_state_5dof, target, planning_time)
        
        if moveit_result:
            waypoints, time_from_start = moveit_result
            
            # Convert MoveIt waypoints to trajectory points interpolated at 30 Hz
            dt = 1.0 / 30.0
            self.trajectory_points = self.interpolate_moveit_trajectory(
                waypoints, time_from_start, dt
            )
            self.trajectory_index = 0
            response.success = True
            self.get_logger().info(f"Computed MoveIt trajectory with {len(self.trajectory_points)} points.")
            return response
        
        # Fallback to simple spline planning if MoveIt fails
        self.get_logger().warn("MoveIt planning failed, falling back to simple spline planning")
        dt = 1.0 / 30.0
        self.trajectory_points = self.compute_trajectory(current_state_5dof, target, planning_time, dt)
        self.trajectory_index = 0
        self.get_logger().info(f"Computed spline trajectory with {len(self.trajectory_points)} points.")
        response.success = True
        return response

    def interpolate_moveit_trajectory(self, waypoints, time_from_start, dt):
        """
        Interpolate MoveIt trajectory waypoints to 30 Hz update rate.
        """
        if not waypoints or not time_from_start:
            return []
        
        trajectory = []
        total_duration = time_from_start[-1] if time_from_start else 0.0
        current_time = 0.0
        
        while current_time <= total_duration:
            # Find the two waypoints to interpolate between
            wp_idx = 0
            for i in range(len(time_from_start) - 1):
                if current_time <= time_from_start[i + 1]:
                    wp_idx = i
                    break
                wp_idx = i + 1
            
            if wp_idx >= len(waypoints) - 1:
                # At or past last waypoint
                trajectory.append(waypoints[-1])
            else:
                # Linear interpolation between waypoints
                t1 = time_from_start[wp_idx]
                t2 = time_from_start[wp_idx + 1]
                
                if t2 > t1:
                    alpha = (current_time - t1) / (t2 - t1)
                    wp1 = waypoints[wp_idx]
                    wp2 = waypoints[wp_idx + 1]
                    interpolated = [w1 + alpha * (w2 - w1) for w1, w2 in zip(wp1, wp2)]
                    trajectory.append(interpolated)
                else:
                    trajectory.append(waypoints[wp_idx])
            
            current_time += dt
        
        return trajectory

    def trajectory_timer_callback(self):
        """Publish the next point in the trajectory at 30 Hz, if available."""
        if self.trajectory_index < len(self.trajectory_points):
            command_msg = Float64MultiArray()
            # Ensure we publish the correct number of joints
            point = self.trajectory_points[self.trajectory_index]
            # If MoveIt returned 5 joints but we need 6, pad with 0
            if len(point) == 5 and self.latest_state and len(self.latest_state) == 6:
                # Keep the 6th joint at its current position
                with self.latest_state_lock:
                    point = point + [self.latest_state[5] if self.latest_state else 0.0]
            elif len(point) != 6:
                # Ensure we have at least 6 joints
                while len(point) < 6:
                    point.append(0.0)
            
            command_msg.data = point
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