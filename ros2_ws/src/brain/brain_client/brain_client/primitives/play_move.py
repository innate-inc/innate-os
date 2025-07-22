#!/usr/bin/env python3
import rclpy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from maurice_msgs.srv import GotoJS
from std_msgs.msg import Float64MultiArray
import time
import math

class PlayMove(Primitive):
    """
    Primitive for playing a chess move by executing a complete pick-and-place sequence.
    """

    # Rest position joint angles
    REST_JOINT_POSITIONS = [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.91425255]
    
    # Movement parameters
    PICK_HEIGHT = 0.05  # Height for picking up pieces
    SAFE_HEIGHT = 0.15  # Safe travel height
    DEFAULT_ORIENTATION = [0.0, math.radians(90), 0.0]  # Roll, Pitch, Yaw in radians

    # Chess coordinates mapping
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

    def __init__(self, logger):
        super().__init__(logger)
        self.ik_delta_publisher = None
        self.goto_js_client = None  # Service client for trajectory execution
        self.last_ik_solution = None  # Store IK solution from robot state
        self.last_ik_timestamp = None  # Track when we last received an IK solution

    @property
    def name(self):
        return "play_move"

    def guidelines(self):
        return (
            "Use this to play a chess move. Provide the move in format 'from_square to_square' "
            "like 'a2 to a4' or 'e1 to e3'. This will calculate the joint positions needed "
            "to move the arm to the specified chess square coordinates."
        )

    def get_required_robot_states(self):
        """Request IK solution from the action server."""
        return [RobotStateType.LAST_IK_SOLUTION]

    def update_robot_state(self, **kwargs):
        """Update the primitive with the latest IK solution."""
        if RobotStateType.LAST_IK_SOLUTION.value in kwargs:
            self.last_ik_solution = kwargs[RobotStateType.LAST_IK_SOLUTION.value]
            # Store the timestamp to detect new solutions
            if self.last_ik_solution and 'header' in self.last_ik_solution:
                stamp = self.last_ik_solution['header']['stamp']
                self.last_ik_timestamp = stamp['sec'] + stamp['nanosec'] / 1e9

    def _parse_chess_move(self, move_str):
        """Parse chess move string like 'a2 to a4' into from and to squares."""
        try:
            parts = move_str.strip().lower().split()
            if len(parts) == 3 and parts[1] == 'to':
                from_square = parts[0]
                to_square = parts[2]
                return from_square, to_square
            else:
                # Try alternative format without 'to'
                if len(parts) == 2:
                    return parts[0], parts[1]
                else:
                    raise ValueError("Invalid move format")
        except Exception as e:
            raise ValueError(f"Could not parse chess move '{move_str}': {e}")

    def _get_coordinates(self, square):
        """Get x, y, z coordinates for a chess square."""
        if square not in self.CHESS_COORDINATES:
            raise ValueError(f"Invalid chess square '{square}'. Valid squares are a1-h8.")
        return self.CHESS_COORDINATES[square]

    def _send_ik_request(self, x, y, z, roll=0.0, pitch=0.0, yaw=0.0):
        """Send IK request via ik_delta topic."""
        if not self.ik_delta_publisher:
            self.ik_delta_publisher = self.node.create_publisher(Twist, 'ik_delta', 10)

        twist_msg = Twist()
        twist_msg.linear.x = x
        twist_msg.linear.y = y
        twist_msg.linear.z = z
        twist_msg.angular.x = roll
        twist_msg.angular.y = pitch
        twist_msg.angular.z = yaw

        self.logger.info(f"Sending IK request for position: x={x:.5f}, y={y:.5f}, z={z:.5f}")
        self.ik_delta_publisher.publish(twist_msg)

    def _wait_for_ik_solution(self, timeout=10.0):
        """Wait for IK solution using the centralized subscription from action server."""
        start_time = time.time()
        # Remember the timestamp of the current solution so we wait for a NEW one
        initial_timestamp = self.last_ik_timestamp
        
        while (time.time() - start_time) < timeout:
            # Check if we have received a NEW IK solution (different timestamp)
            if (self.last_ik_solution is not None and 
                self.last_ik_timestamp is not None and
                self.last_ik_timestamp != initial_timestamp):
                
                # Convert the dict back to a more usable format
                solution = type('IKSolution', (), {})()
                solution.position = self.last_ik_solution['position']
                solution.velocity = self.last_ik_solution['velocity'] 
                solution.effort = self.last_ik_solution['effort']
                
                self.logger.info(f"✅ Using NEW IK solution: {solution.position}")
                
                # Don't clear the solution anymore - just update our reference timestamp
                return solution
            
            # Small delay to avoid busy waiting
            time.sleep(0.1)
        
        return None

    def _solve_ik_for_position(self, x, y, z, roll=0.0, pitch=None, yaw=0.0):
        """Solve IK for a specific position and orientation."""
        if pitch is None:
            pitch = self.DEFAULT_ORIENTATION[1]  # Use default 80-degree pitch
            
        self._send_ik_request(x, y, z, roll, pitch, yaw)
        solution = self._wait_for_ik_solution(timeout=10.0)
        
        if solution is None:
            self.logger.error(f"IK solution failed for position: x={x:.3f}, y={y:.3f}, z={z:.3f}")
            return None
            
        return solution

    def _execute_trajectory(self, joint_positions, trajectory_time=3):
        """Execute a trajectory to the given joint positions."""
        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(GotoJS, 'maurice_arm/goto_js')
            
        if not self.goto_js_client.wait_for_service(timeout_sec=5.0):
            self.logger.error("GotoJS service not available")
            return False

        # Ensure we have 6 joint positions (add 0.0 for 6th joint if needed)
        if len(joint_positions) == 5:
            joint_positions = list(joint_positions) + [0.0]
            
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = trajectory_time

        self.logger.info(f"Executing trajectory: {[f'{pos:.3f}' for pos in joint_positions]}")
        
        # Call service synchronously
        future = self.goto_js_client.call_async(request)
        
        # Wait for service response
        start_time = time.time()
        while not future.done() and (time.time() - start_time) < 10.0:
            time.sleep(0.1)
            
        if future.done():
            response = future.result()
            if response and response.success:
                self.logger.info(f"Trajectory started successfully, waiting {trajectory_time}s for completion")
                time.sleep(trajectory_time + 0.5)  # Wait for trajectory to complete
                return True
            else:
                self.logger.error("Trajectory execution failed")
                return False
        else:
            self.logger.error("Service call timed out")
            return False

    def _move_to_rest(self):
        """Move to the rest position."""
        self._send_feedback("🏠 Moving to rest position")
        return self._execute_trajectory(self.REST_JOINT_POSITIONS, trajectory_time=3)

    def _move_to_position_with_ik(self, x, y, z, description=""):
        """Move to a position using IK and trajectory execution."""
        if description:
            self._send_feedback(f"🎯 {description}")
            
        solution = self._solve_ik_for_position(x, y, z)
        if solution is None:
            return False
            
        return self._execute_trajectory(solution.position, trajectory_time=2)

    def execute(self, **kwargs):
        """
        Execute a complete chess move sequence: rest → from → to → rest.
        
        Args:
            **kwargs: Should contain 'move_str' with chess move in format "a2 to a4" or "a2 a4"
            
        Returns:
            tuple: (result_message, result_status)
        """
        move_str = kwargs.get('move_str', '')
        if not self.node:
            self.logger.error("PlayMove primitive is not functional due to missing ROS node.")
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not move_str:
            self._send_feedback("❌ No chess move provided")
            return "No chess move provided", PrimitiveResult.FAILURE

        try:
            # Parse the chess move
            self._send_feedback(f"🔍 Parsing chess move: '{move_str}'")
            from_square, to_square = self._parse_chess_move(move_str)
            self.logger.info(f"Parsed chess move: {from_square} to {to_square}")
            self._send_feedback(f"✅ Parsed chess move: {from_square} → {to_square}")

            # Get coordinates for both squares
            self._send_feedback(f"📍 Getting coordinates for squares '{from_square}' and '{to_square}'")
            from_coords = self._get_coordinates(from_square)
            to_coords = self._get_coordinates(to_square)
            
            self._send_feedback(f"📋 Executing chess move sequence: {from_square} → {to_square}")
            
            # STEP 1: Move to rest position
            self._send_feedback("1️⃣ Moving to rest position")
            if not self._move_to_rest():
                return f"Failed to move to rest position", PrimitiveResult.FAILURE
                
            # STEP 2: Move up from rest to safe height
            self._send_feedback("2️⃣ Moving up from rest to safe height")
            # Use rest position as reference, but move to safe height
            rest_coords = self._get_coordinates('d4')  # Use center of board as reference
            if not self._move_to_position_with_ik(rest_coords['x'], rest_coords['y'], self.SAFE_HEIGHT, 
                                                  "Moving to safe height above center"):
                return f"Failed to move to safe height", PrimitiveResult.FAILURE
                
            # STEP 3: Move to 'from' coordinates at safe height
            self._send_feedback(f"3️⃣ Moving to {from_square} at safe height")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
                                                  f"Moving above {from_square}"):
                return f"Failed to move above {from_square}", PrimitiveResult.FAILURE
                
            # STEP 4: Move down to pick up piece
            self._send_feedback(f"4️⃣ Moving down to pick up piece at {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.PICK_HEIGHT,
                                                  f"Picking up piece at {from_square}"):
                return f"Failed to pick up piece at {from_square}", PrimitiveResult.FAILURE
                
            # STEP 5: Move up with piece
            self._send_feedback(f"5️⃣ Moving up with piece from {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
                                                  f"Lifting piece from {from_square}"):
                return f"Failed to lift piece from {from_square}", PrimitiveResult.FAILURE
                
            # STEP 6: Move to 'to' coordinates at safe height
            self._send_feedback(f"6️⃣ Moving to {to_square} at safe height")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
                                                  f"Moving above {to_square}"):
                return f"Failed to move above {to_square}", PrimitiveResult.FAILURE
                
            # STEP 7: Move down to place piece
            self._send_feedback(f"7️⃣ Moving down to place piece at {to_square}")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.PICK_HEIGHT,
                                                  f"Placing piece at {to_square}"):
                return f"Failed to place piece at {to_square}", PrimitiveResult.FAILURE
                
            # STEP 8: Move up after placing
            self._send_feedback(f"8️⃣ Moving up after placing piece at {to_square}")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
                                                  f"Lifting from {to_square}"):
                return f"Failed to lift from {to_square}", PrimitiveResult.FAILURE
                
            # STEP 9: Move back to center at safe height
            self._send_feedback("9️⃣ Moving back to center at safe height")
            if not self._move_to_position_with_ik(rest_coords['x'], rest_coords['y'], self.SAFE_HEIGHT,
                                                  "Moving to center at safe height"):
                return f"Failed to move to center", PrimitiveResult.FAILURE
                
            # STEP 10: Final move to rest position
            self._send_feedback("🔟 Moving to final rest position")
            if not self._move_to_rest():
                return f"Failed to move to final rest position", PrimitiveResult.FAILURE
            
            result_msg = f"✅ Chess move {move_str} completed successfully! Executed full pick-and-place sequence."
            self._send_feedback(result_msg)
            self.logger.info(result_msg)
            return result_msg, PrimitiveResult.SUCCESS

        except ValueError as e:
            error_msg = f"Invalid chess move '{move_str}': {e}"
            self._send_feedback(f"❌ {error_msg}")
            self.logger.error(error_msg)
            return error_msg, PrimitiveResult.FAILURE
        except Exception as e:
            error_msg = f"Failed to execute chess move '{move_str}': {e}"
            self._send_feedback(f"❌ {error_msg}")
            self.logger.error(error_msg)
            return error_msg, PrimitiveResult.FAILURE

    def cancel(self):
        """Cancel the chess move operation."""
        self.logger.info("Canceling chess move operation")
        return "Chess move operation canceled" 