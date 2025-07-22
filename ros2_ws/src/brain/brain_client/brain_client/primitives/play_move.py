#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS
from brain_client.primitives.types import Primitive, PrimitiveResult
import threading
import re
import math


class PlayMove(Primitive):
    """
    Primitive for commanding the robot arm to play a chess move.
    This involves moving from a 'from' square to a 'to' square using inverse kinematics.
    """

    # Chess board coordinates mapping
    CHESS_COORDINATES = {
        'a1': {'x': 0.15000, 'y': 0.09800, 'z': 0.02900},
        'a2': {'x': 0.19263, 'y': 0.09790, 'z': 0.03090},
        'a3': {'x': 0.23527, 'y': 0.09780, 'z': 0.03280},
        'a4': {'x': 0.27790, 'y': 0.09770, 'z': 0.03470},
        'a5': {'x': 0.32053, 'y': 0.09760, 'z': 0.03660},
        'a6': {'x': 0.36317, 'y': 0.09750, 'z': 0.03850},
        'a7': {'x': 0.40580, 'y': 0.09740, 'z': 0.04040},
        'a8': {'x': 0.44843, 'y': 0.09730, 'z': 0.04230},
        'b1': {'x': 0.14971, 'y': 0.05543, 'z': 0.02914},
        'b2': {'x': 0.19235, 'y': 0.05533, 'z': 0.03104},
        'b3': {'x': 0.23498, 'y': 0.05523, 'z': 0.03294},
        'b4': {'x': 0.27761, 'y': 0.05513, 'z': 0.03484},
        'b5': {'x': 0.32025, 'y': 0.05503, 'z': 0.03674},
        'b6': {'x': 0.36288, 'y': 0.05493, 'z': 0.03864},
        'b7': {'x': 0.40551, 'y': 0.05483, 'z': 0.04054},
        'b8': {'x': 0.44815, 'y': 0.05473, 'z': 0.04244},
        'c1': {'x': 0.14943, 'y': 0.01286, 'z': 0.02929},
        'c2': {'x': 0.19206, 'y': 0.01276, 'z': 0.03119},
        'c3': {'x': 0.23470, 'y': 0.01266, 'z': 0.03309},
        'c4': {'x': 0.27733, 'y': 0.01256, 'z': 0.03499},
        'c5': {'x': 0.31996, 'y': 0.01246, 'z': 0.03689},
        'c6': {'x': 0.36260, 'y': 0.01236, 'z': 0.03879},
        'c7': {'x': 0.40523, 'y': 0.01226, 'z': 0.04069},
        'c8': {'x': 0.44786, 'y': 0.01216, 'z': 0.04259},
        'd1': {'x': 0.14914, 'y': -0.02971, 'z': 0.02943},
        'd2': {'x': 0.19178, 'y': -0.02981, 'z': 0.03133},
        'd3': {'x': 0.23441, 'y': -0.02991, 'z': 0.03323},
        'd4': {'x': 0.27704, 'y': -0.03001, 'z': 0.03513},
        'd5': {'x': 0.31968, 'y': -0.03011, 'z': 0.03703},
        'd6': {'x': 0.36231, 'y': -0.03021, 'z': 0.03893},
        'd7': {'x': 0.40494, 'y': -0.03031, 'z': 0.04083},
        'd8': {'x': 0.44758, 'y': -0.03041, 'z': 0.04273},
        'e1': {'x': 0.14886, 'y': -0.07229, 'z': 0.02957},
        'e2': {'x': 0.19149, 'y': -0.07239, 'z': 0.03147},
        'e3': {'x': 0.23412, 'y': -0.07249, 'z': 0.03337},
        'e4': {'x': 0.27676, 'y': -0.07259, 'z': 0.03527},
        'e5': {'x': 0.31939, 'y': -0.07269, 'z': 0.03717},
        'e6': {'x': 0.36202, 'y': -0.07279, 'z': 0.03907},
        'e7': {'x': 0.40466, 'y': -0.07289, 'z': 0.04097},
        'e8': {'x': 0.44729, 'y': -0.07299, 'z': 0.04287},
        'f1': {'x': 0.14857, 'y': -0.11486, 'z': 0.02971},
        'f2': {'x': 0.19120, 'y': -0.11496, 'z': 0.03161},
        'f3': {'x': 0.23384, 'y': -0.11506, 'z': 0.03351},
        'f4': {'x': 0.27647, 'y': -0.11516, 'z': 0.03541},
        'f5': {'x': 0.31910, 'y': -0.11526, 'z': 0.03731},
        'f6': {'x': 0.36174, 'y': -0.11536, 'z': 0.03921},
        'f7': {'x': 0.40437, 'y': -0.11546, 'z': 0.04111},
        'f8': {'x': 0.44700, 'y': -0.11556, 'z': 0.04301},
        'g1': {'x': 0.14829, 'y': -0.15743, 'z': 0.02986},
        'g2': {'x': 0.19092, 'y': -0.15753, 'z': 0.03176},
        'g3': {'x': 0.23355, 'y': -0.15763, 'z': 0.03366},
        'g4': {'x': 0.27619, 'y': -0.15773, 'z': 0.03556},
        'g5': {'x': 0.31882, 'y': -0.15783, 'z': 0.03746},
        'g6': {'x': 0.36145, 'y': -0.15793, 'z': 0.03936},
        'g7': {'x': 0.40409, 'y': -0.15803, 'z': 0.04126},
        'g8': {'x': 0.44672, 'y': -0.15813, 'z': 0.04316},
        'h1': {'x': 0.14800, 'y': -0.20000, 'z': 0.03000},
        'h2': {'x': 0.19063, 'y': -0.20010, 'z': 0.03190},
        'h3': {'x': 0.23327, 'y': -0.20020, 'z': 0.03380},
        'h4': {'x': 0.27590, 'y': -0.20030, 'z': 0.03570},
        'h5': {'x': 0.31853, 'y': -0.20040, 'z': 0.03760},
        'h6': {'x': 0.36117, 'y': -0.20050, 'z': 0.03950},
        'h7': {'x': 0.40380, 'y': -0.20060, 'z': 0.04140},
        'h8': {'x': 0.44643, 'y': -0.20070, 'z': 0.04330},
    }

    # Rest position joint angles (from your service call)
    REST_JOINT_POSITIONS = [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.91425255]

    def __init__(self, logger):
        super().__init__(logger)
        self.ik_delta_publisher = None
        self.ik_solution_subscriber = None
        self.goto_js_client = None
        self.latest_ik_solution = None
        self.ik_solution_received = threading.Event()

    @property
    def name(self):
        return "play_move"

    def guidelines(self):
        return (
            "Use this command to play a chess move. Specify the move in chess notation "
            "like 'e2 to e4' or 'a1 to f2'. The robot will pick up the piece from the "
            "source square and place it on the destination square."
        )

    def _parse_move(self, move_string):
        """
        Parse a chess move string like 'e2 to e4' or 'a1 f2'.
        Returns tuple (from_square, to_square) or (None, None) if invalid.
        """
        # Remove extra whitespace and convert to lowercase
        move_string = move_string.strip().lower()
        
        # Try different patterns
        patterns = [
            r'([a-h][1-8])\s+to\s+([a-h][1-8])',  # "e2 to e4"
            r'([a-h][1-8])\s+([a-h][1-8])',       # "e2 e4"
            r'([a-h][1-8])-([a-h][1-8])',         # "e2-e4"
        ]
        
        for pattern in patterns:
            match = re.match(pattern, move_string)
            if match:
                from_square, to_square = match.groups()
                if from_square in self.CHESS_COORDINATES and to_square in self.CHESS_COORDINATES:
                    return from_square, to_square
        
        return None, None

    def _wait_for_services(self):
        """Wait for required services to be available"""
        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(GotoJS, 'maurice_arm/goto_js')
        
        if not self.goto_js_client.wait_for_service(timeout_sec=10.0):
            self.logger.error("goto_js service not available")
            return False
        return True

    def _ik_solution_callback(self, msg: JointState):
        """Store the latest IK solution"""
        self.latest_ik_solution = msg
        self.ik_solution_received.set()
        self.logger.info(f"✅ Received IK solution: {[f'{pos:.3f}' for pos in msg.position]}")

    def _solve_ik_for_position(self, target_x, target_y, target_z, target_roll=0.0, target_pitch=math.radians(80), target_yaw=0.0, timeout=10.0):
        """
        Solve IK for a target position using the IK delta topic.
        Returns True if successful, False otherwise.
        """
        # Initialize publishers and subscribers ONCE in execute method, not here
        # This should be moved to execute() method to avoid creating multiple instances

        # Create and publish Twist message with absolute pose values
        twist_msg = Twist()
        twist_msg.linear.x = target_x
        twist_msg.linear.y = target_y
        twist_msg.linear.z = target_z
        twist_msg.angular.x = target_roll
        twist_msg.angular.y = target_pitch
        twist_msg.angular.z = target_yaw

        self.logger.info(f"Solving IK for position: x={target_x:.3f}, y={target_y:.3f}, z={target_z:.3f}")
        self.logger.info(f"Orientation: roll={target_roll:.3f}, pitch={target_pitch:.3f}, yaw={target_yaw:.3f}")
        self.logger.info(f"Waiting up to {timeout} seconds for IK solution...")

        # Reset flag and publish
        self.ik_solution_received.clear()
        self.ik_delta_publisher.publish(twist_msg)
        
        # Small delay to ensure message is sent
        time.sleep(0.1)

        # Wait for IK solution with active spinning
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            # Actively spin the node to process callbacks
            rclpy.spin_once(self.node, timeout_sec=0.1)
            
            # Check if we received the solution
            if self.ik_solution_received.is_set():
                self.logger.info(f"✅ IK solution received successfully after {time.time() - start_time:.2f}s")
                return True
            
            # Small sleep to prevent busy waiting
            time.sleep(0.05)
        
        # Final check after timeout
        self.logger.warn(f"⏰ IK solution timeout after {timeout}s, doing final check...")
        rclpy.spin_once(self.node, timeout_sec=0.5)  # One more spin with longer timeout
        
        if self.ik_solution_received.is_set():
            self.logger.warn("⚠️  IK solution received after timeout, but proceeding")
            return True
        else:
            self.logger.error("❌ IK solution not received within timeout")
            return False

    def _execute_trajectory_to_ik_solution(self, trajectory_time=3):
        """Execute trajectory to the latest IK solution"""
        if not self.latest_ik_solution:
            self.logger.error("No IK solution available")
            return False

        # Create service request
        request = GotoJS.Request()
        request.data = Float64MultiArray()

        # IK returns 5 positions, append 0.0 for the 6th joint
        ik_positions = list(self.latest_ik_solution.position)
        if len(ik_positions) == 5:
            ik_positions.append(0.0)  # Add 6th joint position as 0.0

        request.data.data = ik_positions
        request.time = trajectory_time

        self.logger.info(f"Executing trajectory to joint positions: {[f'{pos:.3f}' for pos in request.data.data]}")

        # Call service
        future = self.goto_js_client.call_async(request)
        rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)

        if future.result() is not None:
            response = future.result()
            if response.success:
                self.logger.info("Trajectory execution started successfully")
                # Wait for trajectory to complete
                time.sleep(trajectory_time + 0.5)
                return True
            else:
                self.logger.error("Trajectory execution failed")
                return False
        else:
            self.logger.error("Service call failed or timed out")
            return False

    def _go_to_rest_position(self):
        """Move arm to rest position using direct joint angles"""
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = self.REST_JOINT_POSITIONS
        request.time = 5

        self.logger.info("Moving to rest position")
        
        # Call service
        future = self.goto_js_client.call_async(request)
        rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)

        if future.result() is not None:
            response = future.result()
            if response.success:
                self.logger.info("Rest position trajectory started successfully")
                time.sleep(5.5)  # Wait for trajectory to complete
                return True
            else:
                self.logger.error("Rest position trajectory failed")
                return False
        else:
            self.logger.error("Rest position service call failed or timed out")
            return False

    def _move_to_position(self, target_position, description=""):
        """
        Move the arm to a target position using inverse kinematics.
        target_position should be a dict with 'x', 'y', 'z' keys.
        """
        target_x = target_position['x']
        target_y = target_position['y']
        target_z = target_position['z']

        self.logger.info(f"{description} - Moving to position: x={target_x:.4f}, y={target_y:.4f}, z={target_z:.4f}")

        # Solve IK for the target position
        if not self._solve_ik_for_position(target_x, target_y, target_z):
            return False

        # Execute trajectory to the IK solution
        if not self._execute_trajectory_to_ik_solution():
            return False

        return True

    def execute(self, move_string=""):
        """
        Execute the chess move.
        
        Args:
            move_string: Chess move in notation like 'e2 to e4'
        """
        # RESET STATE at the beginning of each execution
        self.latest_ik_solution = None
        self.ik_solution_received.clear()  # Clear the threading event
        
        if not self.node:
            self.logger.error("PlayMove primitive is not functional due to missing ROS node.")
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        # Initialize publishers and subscribers ONCE per execution to avoid race conditions
        if not self.ik_delta_publisher:
            self.ik_delta_publisher = self.node.create_publisher(Twist, 'ik_delta', 10)
        
        if not self.ik_solution_subscriber:
            self.ik_solution_subscriber = self.node.create_subscription(
                JointState,
                'ik_solution',
                self._ik_solution_callback,
                10
            )

        if not move_string:
            return "No move specified", PrimitiveResult.FAILURE

        # Parse the move
        from_square, to_square = self._parse_move(move_string)
        if not from_square or not to_square:
            return f"Invalid move format: '{move_string}'. Use format like 'e2 to e4'", PrimitiveResult.FAILURE

        self.logger.info(f"\033[96m[BrainClient] Initiating chess move: {from_square} to {to_square}\033[0m")

        # Wait for services to be available
        if not self._wait_for_services():
            return "Required services not available", PrimitiveResult.FAILURE

        try:
            # Step 1: Move above the 'from' square at z=0.15
            from_coords = self.CHESS_COORDINATES[from_square].copy()
            from_coords['z'] = from_coords['z'] + 0.12  # Add 0.12m above the square
            
            self._send_feedback(f"Moving above {from_square} square...")
            if not self._move_to_position(from_coords, f"Step 1: Above {from_square}"):
                return f"Failed to move above {from_square} square", PrimitiveResult.FAILURE

            # Step 2: Lower to z=0.05 to pick up the piece
            from_coords['z'] = from_coords['z'] - 0.10  # Lower by 0.10m (from 0.12 above to 0.02 above)
            
            self._send_feedback(f"Lowering to pick up piece from {from_square}...")
            if not self._move_to_position(from_coords, f"Step 2: Pick up from {from_square}"):
                return f"Failed to lower to {from_square} square", PrimitiveResult.FAILURE

            # Step 3: Raise back up
            from_coords['z'] = from_coords['z'] + 0.10  # Raise back up
            
            self._send_feedback("Lifting piece...")
            if not self._move_to_position(from_coords, f"Step 3: Lift from {from_square}"):
                return f"Failed to lift from {from_square} square", PrimitiveResult.FAILURE

            # Step 4: Move above the 'to' square at z=0.15
            to_coords = self.CHESS_COORDINATES[to_square].copy()
            to_coords['z'] = to_coords['z'] + 0.12  # Add 0.12m above the square
            
            self._send_feedback(f"Moving to {to_square} square...")
            if not self._move_to_position(to_coords, f"Step 4: Above {to_square}"):
                return f"Failed to move above {to_square} square", PrimitiveResult.FAILURE

            # Step 5: Lower to place the piece
            to_coords['z'] = to_coords['z'] - 0.10  # Lower by 0.10m
            
            self._send_feedback(f"Placing piece on {to_square}...")
            if not self._move_to_position(to_coords, f"Step 5: Place on {to_square}"):
                return f"Failed to lower to {to_square} square", PrimitiveResult.FAILURE

            # Step 6: Raise back up
            to_coords['z'] = to_coords['z'] + 0.10  # Raise back up
            
            self._send_feedback("Releasing piece...")
            if not self._move_to_position(to_coords, f"Step 6: Release at {to_square}"):
                return f"Failed to lift from {to_square} square", PrimitiveResult.FAILURE

            # Step 7: Return to rest position
            self._send_feedback("Returning to rest position...")
            if not self._go_to_rest_position():
                return "Failed to return to rest position", PrimitiveResult.FAILURE

            self.logger.info(f"\033[92m[BrainClient] Chess move {from_square} to {to_square} completed successfully.\033[0m")
            return f"Chess move {from_square} to {to_square} completed successfully.", PrimitiveResult.SUCCESS

        except Exception as e:
            self.logger.error(f"Error during chess move execution: {e}")
            return f"Error during chess move execution: {e}", PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancel the chess move operation.
        """
        self.logger.info("\033[91m[BrainClient] Chess move operation cancellation requested.\033[0m")
        # For IK delta commands, we can't directly cancel them once sent,
        # but we could potentially send a command to return to rest position
        return "Chess move operation cancellation requested. The arm will complete its current movement." 