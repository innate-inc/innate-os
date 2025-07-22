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
    
    # Rest position coordinates (actual end-effector position at rest)
    REST_POSITION = {'x': 0.08507, 'y': 0.09278, 'z': 0.0527}
    
    # Movement parameters
    PICK_HEIGHT = 0.08  # Height for picking up pieces
    SAFE_HEIGHT = 0.15  # Safe travel height
    DEFAULT_ORIENTATION = [0.0, math.radians(90), 0.0]  # Roll, Pitch, Yaw in radians
    
    # Gripper control
    GRIPPER_OPEN = 0.6   # Joint 6 position for open gripper
    GRIPPER_CLOSED = 0.0 # Joint 6 position for closed gripper

    # Chess coordinates mapping
    CHESS_COORDINATES = {
    'a1': {'x': 0.15340, 'y': 0.10068, 'z': 0.04155},
    'a2': {'x': 0.19776, 'y': 0.09847, 'z': 0.04659},
    'a3': {'x': 0.24212, 'y': 0.09626, 'z': 0.05163},
    'a4': {'x': 0.28648, 'y': 0.09404, 'z': 0.05668},
    'a5': {'x': 0.33084, 'y': 0.09183, 'z': 0.06172},
    'a6': {'x': 0.37520, 'y': 0.08962, 'z': 0.06676},
    'a7': {'x': 0.41956, 'y': 0.08741, 'z': 0.07180},
    'a8': {'x': 0.46392, 'y': 0.08520, 'z': 0.07684},
    'b1': {'x': 0.15125, 'y': 0.05550, 'z': 0.04120},
    'b2': {'x': 0.19561, 'y': 0.05329, 'z': 0.04624},
    'b3': {'x': 0.23997, 'y': 0.05107, 'z': 0.05128},
    'b4': {'x': 0.28433, 'y': 0.04886, 'z': 0.05632},
    'b5': {'x': 0.32869, 'y': 0.04665, 'z': 0.06136},
    'b6': {'x': 0.37305, 'y': 0.04444, 'z': 0.06641},
    'b7': {'x': 0.41741, 'y': 0.04223, 'z': 0.07145},
    'b8': {'x': 0.46177, 'y': 0.04002, 'z': 0.07649},
    'c1': {'x': 0.14911, 'y': 0.01031, 'z': 0.04085},
    'c2': {'x': 0.19347, 'y': 0.00810, 'z': 0.04589},
    'c3': {'x': 0.23783, 'y': 0.00589, 'z': 0.05093},
    'c4': {'x': 0.28219, 'y': 0.00368, 'z': 0.05597},
    'c5': {'x': 0.32655, 'y': 0.00147, 'z': 0.06101},
    'c6': {'x': 0.37091, 'y': -0.00074, 'z': 0.06605},
    'c7': {'x': 0.41527, 'y': -0.00296, 'z': 0.07110},
    'c8': {'x': 0.45963, 'y': -0.00517, 'z': 0.07614},
    'd1': {'x': 0.14696, 'y': -0.03487, 'z': 0.04049},
    'd2': {'x': 0.19132, 'y': -0.03708, 'z': 0.04554},
    'd3': {'x': 0.23568, 'y': -0.03929, 'z': 0.05058},
    'd4': {'x': 0.28004, 'y': -0.04150, 'z': 0.05562},
    'd5': {'x': 0.32440, 'y': -0.04372, 'z': 0.06066},
    'd6': {'x': 0.36876, 'y': -0.04593, 'z': 0.06570},
    'd7': {'x': 0.41312, 'y': -0.04814, 'z': 0.07074},
    'd8': {'x': 0.45748, 'y': -0.05035, 'z': 0.07579},
    'e1': {'x': 0.14481, 'y': -0.08005, 'z': 0.04014},
    'e2': {'x': 0.18917, 'y': -0.08226, 'z': 0.04518},
    'e3': {'x': 0.23353, 'y': -0.08447, 'z': 0.05022},
    'e4': {'x': 0.27789, 'y': -0.08669, 'z': 0.05527},
    'e5': {'x': 0.32225, 'y': -0.08890, 'z': 0.06031},
    'e6': {'x': 0.36661, 'y': -0.09111, 'z': 0.06535},
    'e7': {'x': 0.41097, 'y': -0.09332, 'z': 0.07039},
    'e8': {'x': 0.45533, 'y': -0.09553, 'z': 0.07543},
    'f1': {'x': 0.14266, 'y': -0.12523, 'z': 0.03979},
    'f2': {'x': 0.18702, 'y': -0.12745, 'z': 0.04483},
    'f3': {'x': 0.23138, 'y': -0.12966, 'z': 0.04987},
    'f4': {'x': 0.27574, 'y': -0.13187, 'z': 0.05491},
    'f5': {'x': 0.32010, 'y': -0.13408, 'z': 0.05996},
    'f6': {'x': 0.36446, 'y': -0.13629, 'z': 0.06500},
    'f7': {'x': 0.40882, 'y': -0.13850, 'z': 0.07004},
    'f8': {'x': 0.45318, 'y': -0.14072, 'z': 0.07508},
    'g1': {'x': 0.14052, 'y': -0.17042, 'z': 0.03944},
    'g2': {'x': 0.18488, 'y': -0.17263, 'z': 0.04448},
    'g3': {'x': 0.22924, 'y': -0.17484, 'z': 0.04952},
    'g4': {'x': 0.27360, 'y': -0.17705, 'z': 0.05456},
    'g5': {'x': 0.31796, 'y': -0.17926, 'z': 0.05960},
    'g6': {'x': 0.36232, 'y': -0.18148, 'z': 0.06465},
    'g7': {'x': 0.40668, 'y': -0.18369, 'z': 0.06969},
    'g8': {'x': 0.45104, 'y': -0.18590, 'z': 0.07473},
    'h1': {'x': 0.13837, 'y': -0.21560, 'z': 0.03909},
    'h2': {'x': 0.18273, 'y': -0.21781, 'z': 0.04413},
    'h3': {'x': 0.22709, 'y': -0.22002, 'z': 0.04917},
    'h4': {'x': 0.27145, 'y': -0.22224, 'z': 0.05421},
    'h5': {'x': 0.31581, 'y': -0.22445, 'z': 0.05925},
    'h6': {'x': 0.36017, 'y': -0.22666, 'z': 0.06429},
    'h7': {'x': 0.40453, 'y': -0.22887, 'z': 0.06934},
    'h8': {'x': 0.44889, 'y': -0.23108, 'z': 0.07438}
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

    def _execute_trajectory(self, joint_positions, trajectory_time=3, gripper_state=None):
        """Execute a trajectory to the given joint positions with optional gripper control."""
        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(GotoJS, 'maurice_arm/goto_js')
            
        if not self.goto_js_client.wait_for_service(timeout_sec=5.0):
            self.logger.error("GotoJS service not available")
            return False

        # Ensure we have 6 joint positions
        if len(joint_positions) == 5:
            joint_positions = list(joint_positions) + [0.0]
        else:
            joint_positions = list(joint_positions)
            
        # Set gripper state if specified
        if gripper_state is not None:
            joint_positions[5] = gripper_state  # Set the 6th joint (gripper)
            
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = trajectory_time

        gripper_desc = ""
        if gripper_state == self.GRIPPER_OPEN:
            gripper_desc = " (gripper OPEN)"
        elif gripper_state == self.GRIPPER_CLOSED:
            gripper_desc = " (gripper CLOSED)"

        self.logger.info(f"Executing trajectory: {[f'{pos:.3f}' for pos in joint_positions]}{gripper_desc}")
        
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

    def _move_to_position_with_ik(self, x, y, z, description="", gripper_state=None):
        """Move to a position using IK and trajectory execution with optional gripper control."""
        if description:
            self._send_feedback(f"🎯 {description}")
            
        solution = self._solve_ik_for_position(x, y, z)
        if solution is None:
            return False
            
        return self._execute_trajectory(solution.position, trajectory_time=2, gripper_state=gripper_state)

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
            # Use actual rest position coordinates, but move to safe height
            if not self._move_to_position_with_ik(self.REST_POSITION['x'], self.REST_POSITION['y'], self.SAFE_HEIGHT, 
                                                  "Moving to safe height above rest position"):
                return f"Failed to move to safe height", PrimitiveResult.FAILURE
                
            # STEP 3: Move to 'from' coordinates at safe height
            self._send_feedback(f"3️⃣ Moving to {from_square} at safe height")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
                                                  f"Moving above {from_square}"):
                return f"Failed to move above {from_square}", PrimitiveResult.FAILURE
                
            # STEP 3.5: Open gripper before going down
            self._send_feedback(f"🤏 Opening gripper above {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
                                                  f"Opening gripper above {from_square}", 
                                                  gripper_state=self.GRIPPER_OPEN):
                return f"Failed to open gripper above {from_square}", PrimitiveResult.FAILURE
                
            # STEP 4: Move down to pick up piece
            self._send_feedback(f"4️⃣ Moving down to pick up piece at {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.PICK_HEIGHT,
                                                  f"Moving down to {from_square}", 
                                                  gripper_state=self.GRIPPER_OPEN):
                return f"Failed to move down to {from_square}", PrimitiveResult.FAILURE
                
            # STEP 4.5: Close gripper to grab piece
            self._send_feedback(f"✊ Closing gripper to grab piece at {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.PICK_HEIGHT,
                                                  f"Grabbing piece at {from_square}", 
                                                  gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to grab piece at {from_square}", PrimitiveResult.FAILURE
                
            # STEP 5: Move up with piece
            self._send_feedback(f"5️⃣ Moving up with piece from {from_square}")
            if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
                                                  f"Lifting piece from {from_square}", 
                                                  gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to lift piece from {from_square}", PrimitiveResult.FAILURE
                
            # STEP 6: Move to 'to' coordinates at safe height
            self._send_feedback(f"6️⃣ Moving to {to_square} at safe height")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
                                                  f"Moving above {to_square}", 
                                                  gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to move above {to_square}", PrimitiveResult.FAILURE
                
            # STEP 7: Move down to place piece
            self._send_feedback(f"7️⃣ Moving down to place piece at {to_square}")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.PICK_HEIGHT,
                                                  f"Moving down to {to_square}", 
                                                  gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to move down to {to_square}", PrimitiveResult.FAILURE
                
            # STEP 7.5: Open gripper to release piece
            self._send_feedback(f"🤏 Opening gripper to release piece at {to_square}")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.PICK_HEIGHT,
                                                  f"Releasing piece at {to_square}", 
                                                  gripper_state=self.GRIPPER_OPEN):
                return f"Failed to release piece at {to_square}", PrimitiveResult.FAILURE
                
            # STEP 8: Move up after placing
            self._send_feedback(f"8️⃣ Moving up after placing piece at {to_square}")
            if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
                                                  f"Lifting from {to_square}", 
                                                  gripper_state=self.GRIPPER_OPEN):
                return f"Failed to lift from {to_square}", PrimitiveResult.FAILURE
                
            # STEP 9: Move back to rest position at safe height
            self._send_feedback("9️⃣ Moving back to rest position at safe height")
            if not self._move_to_position_with_ik(self.REST_POSITION['x'], self.REST_POSITION['y'], self.SAFE_HEIGHT,
                                                  "Moving to rest position at safe height"):
                return f"Failed to move to rest position at safe height", PrimitiveResult.FAILURE
                
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