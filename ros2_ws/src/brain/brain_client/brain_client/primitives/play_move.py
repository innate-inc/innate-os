#!/usr/bin/env python3
import rclpy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from maurice_msgs.srv import GotoJS
from std_msgs.msg import Float64MultiArray
import time
import math
import cv2
import tempfile
import os
from brain_client.utils.camera_utils import initialize_camera

class PlayMove(Primitive):
    """
    Primitive for playing a chess move by executing a complete pick-and-place sequence.
    This primitive now captures an image AFTER the move is completed.
    """

    # Rest position joint angles
    REST_JOINT_POSITIONS = [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.91425255]
    
    # Rest position coordinates (actual end-effector position at rest)
    REST_POSITION = {'x': 0.08507, 'y': 0.09278, 'z': 0.0527}
    
    # Movement parameters
    PICK_HEIGHT = 0.08  # Height for picking up pieces
    SAFE_HEIGHT = 0.15  # Safe travel height
    DEFAULT_ORIENTATION = [0.0, math.radians(80), 0.0]  # Roll, Pitch, Yaw in radians
    
    # Gripper control
    GRIPPER_OPEN = 0.6   # Joint 6 position for open gripper
    GRIPPER_CLOSED = 0.0 # Joint 6 position for closed gripper

    # Chess coordinates mapping
    CHESS_COORDINATES = {
    'a1': {'x': 0.15497, 'y': 0.10422, 'z': 0.05126},
    'a2': {'x': 0.19796, 'y': 0.10312, 'z': 0.05686},
    'a3': {'x': 0.24096, 'y': 0.10201, 'z': 0.06245},
    'a4': {'x': 0.28395, 'y': 0.10090, 'z': 0.06805},
    'a5': {'x': 0.32694, 'y': 0.09979, 'z': 0.07365},
    'a6': {'x': 0.36994, 'y': 0.09868, 'z': 0.07924},
    'a7': {'x': 0.41293, 'y': 0.09758, 'z': 0.08484},
    'a8': {'x': 0.45592, 'y': 0.09647, 'z': 0.09044},
    'b1': {'x': 0.15322, 'y': 0.05796, 'z': 0.05115},
    'b2': {'x': 0.19622, 'y': 0.05685, 'z': 0.05675},
    'b3': {'x': 0.23921, 'y': 0.05574, 'z': 0.06235},
    'b4': {'x': 0.28220, 'y': 0.05463, 'z': 0.06794},
    'b5': {'x': 0.32520, 'y': 0.05353, 'z': 0.07354},
    'b6': {'x': 0.36819, 'y': 0.05242, 'z': 0.07914},
    'b7': {'x': 0.41118, 'y': 0.05131, 'z': 0.08473},
    'b8': {'x': 0.45418, 'y': 0.05020, 'z': 0.09033},
    'c1': {'x': 0.15148, 'y': 0.01169, 'z': 0.05104},
    'c2': {'x': 0.19447, 'y': 0.01058, 'z': 0.05664},
    'c3': {'x': 0.23746, 'y': 0.00947, 'z': 0.06224},
    'c4': {'x': 0.28046, 'y': 0.00837, 'z': 0.06784},
    'c5': {'x': 0.32345, 'y': 0.00726, 'z': 0.07343},
    'c6': {'x': 0.36644, 'y': 0.00615, 'z': 0.07903},
    'c7': {'x': 0.40944, 'y': 0.00504, 'z': 0.08463},
    'c8': {'x': 0.45243, 'y': 0.00393, 'z': 0.09022},
    'd1': {'x': 0.14973, 'y': -0.03458, 'z': 0.05094},
    'd2': {'x': 0.19272, 'y': -0.03569, 'z': 0.05653},
    'd3': {'x': 0.23572, 'y': -0.03679, 'z': 0.06213},
    'd4': {'x': 0.27871, 'y': -0.03790, 'z': 0.06773},
    'd5': {'x': 0.32170, 'y': -0.03901, 'z': 0.07333},
    'd6': {'x': 0.36470, 'y': -0.04012, 'z': 0.07892},
    'd7': {'x': 0.40769, 'y': -0.04122, 'z': 0.08452},
    'd8': {'x': 0.45068, 'y': -0.04233, 'z': 0.09012},
    'e1': {'x': 0.14798, 'y': -0.08084, 'z': 0.05083},
    'e2': {'x': 0.19098, 'y': -0.08195, 'z': 0.05643},
    'e3': {'x': 0.23397, 'y': -0.08306, 'z': 0.06202},
    'e4': {'x': 0.27696, 'y': -0.08417, 'z': 0.06762},
    'e5': {'x': 0.31996, 'y': -0.08528, 'z': 0.07322},
    'e6': {'x': 0.36295, 'y': -0.08638, 'z': 0.07882},
    'e7': {'x': 0.40594, 'y': -0.08749, 'z': 0.08441},
    'e8': {'x': 0.44894, 'y': -0.08860, 'z': 0.09001},
    'f1': {'x': 0.14624, 'y': -0.12711, 'z': 0.05072},
    'f2': {'x': 0.18923, 'y': -0.12822, 'z': 0.05632},
    'f3': {'x': 0.23222, 'y': -0.12933, 'z': 0.06192},
    'f4': {'x': 0.27522, 'y': -0.13043, 'z': 0.06751},
    'f5': {'x': 0.31821, 'y': -0.13154, 'z': 0.07311},
    'f6': {'x': 0.36120, 'y': -0.13265, 'z': 0.07871},
    'f7': {'x': 0.40420, 'y': -0.13376, 'z': 0.08431},
    'f8': {'x': 0.44719, 'y': -0.13487, 'z': 0.08990},
    'g1': {'x': 0.14449, 'y': -0.17338, 'z': 0.05062},
    'g2': {'x': 0.18748, 'y': -0.17449, 'z': 0.05621},
    'g3': {'x': 0.23048, 'y': -0.17559, 'z': 0.06181},
    'g4': {'x': 0.27347, 'y': -0.17670, 'z': 0.06741},
    'g5': {'x': 0.31646, 'y': -0.17781, 'z': 0.07300},
    'g6': {'x': 0.35946, 'y': -0.17892, 'z': 0.07860},
    'g7': {'x': 0.40245, 'y': -0.18003, 'z': 0.08420},
    'g8': {'x': 0.44544, 'y': -0.18113, 'z': 0.08980},
    'h1': {'x': 0.14274, 'y': -0.21965, 'z': 0.05051},
    'h2': {'x': 0.18574, 'y': -0.22075, 'z': 0.05611},
    'h3': {'x': 0.22873, 'y': -0.22186, 'z': 0.06170},
    'h4': {'x': 0.27172, 'y': -0.22297, 'z': 0.06730},
    'h5': {'x': 0.31472, 'y': -0.22408, 'z': 0.07290},
    'h6': {'x': 0.35771, 'y': -0.22518, 'z': 0.07849},
    'h7': {'x': 0.40070, 'y': -0.22629, 'z': 0.08409},
    'h8': {'x': 0.44370, 'y': -0.22740, 'z': 0.08969},
}



    def __init__(self, logger):
        super().__init__(logger)
        self.ik_delta_publisher = None
        self.goto_js_client = None  # Service client for trajectory execution
        self.last_ik_solution = None  # Store IK solution from robot state
        self.last_ik_timestamp = None  # Track when we last received an IK solution
        
        # Camera configuration for image capture
        self.camera_index = None  # Will be determined automatically
        self.camera = None
        self.preferred_backend = cv2.CAP_V4L2  # Use V4L2 backend for better compatibility
        
        # Store path to the "after" image for get_chess_move to use
        self.after_move_image_path = None

    @property
    def name(self):
        return "play_move"

    def guidelines(self):
        return (
            "Use this to play a chess move. Provide the move in format 'from_square to_square' "
            "like 'a2 to a4' or 'e1 to e3'. This will calculate the joint positions needed "
            "to move the arm to the specified chess square coordinates. After the move, it "
            "will capture an image of the board for vision analysis."
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

    def _initialize_camera(self):
        """Initialize the camera for capturing images."""
        if self.camera is not None and self.camera.isOpened():
            return True  # Already initialized
            
        self.camera, self.camera_index = initialize_camera(self.logger, self.camera_index, self.preferred_backend)
        return self.camera is not None

    def _capture_after_image(self):
        """Capture an image after the move is completed and store the path."""
        if not self._initialize_camera():
            return None
        
        try:
            # Capture frame
            ret, frame = self.camera.read()
            
            # Immediately release camera after capture to prevent blocking
            self.camera.release()
            cv2.destroyAllWindows()
            self.camera = None
            self.logger.debug("Camera released immediately after capture")
            
            if not ret or frame is None:
                self.logger.error("❌ Failed to capture after-move frame from camera")
                return None
            
            # Create temporary file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='chess_after_move_')
            
            # Save the captured frame
            success = cv2.imwrite(temp_path, frame)
            os.close(temp_fd)  # Close the file descriptor
            
            if not success:
                self.logger.error("❌ Failed to save after-move image")
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                return None
            
            self.logger.info(f"📸 Captured after-move image and saved to: {temp_path}")
            self.after_move_image_path = temp_path  # Store for get_chess_move to use
            return temp_path
            
        except Exception as e:
            self.logger.error(f"❌ Error capturing after-move image: {e}")
            # Make sure camera is released even on error
            if self.camera is not None:
                try:
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                except:
                    pass
            return None

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
            
            # # STEP 1: Move to rest position
            # self._send_feedback("1️⃣ Moving to rest position")
            # if not self._move_to_rest():
            #     return f"Failed to move to rest position", PrimitiveResult.FAILURE
                
            # # STEP 2: Move up from rest to safe height
            # self._send_feedback("2️⃣ Moving up from rest to safe height")
            # # Use actual rest position coordinates, but move to safe height
            # if not self._move_to_position_with_ik(self.REST_POSITION['x'], self.REST_POSITION['y'], self.SAFE_HEIGHT, 
            #                                       "Moving to safe height above rest position"):
            #     return f"Failed to move to safe height", PrimitiveResult.FAILURE
                
            # # STEP 3: Move to 'from' coordinates at safe height
            # self._send_feedback(f"3️⃣ Moving to {from_square} at safe height")
            # if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
            #                                       f"Moving above {from_square}"):
            #     return f"Failed to move above {from_square}", PrimitiveResult.FAILURE
                
            # # STEP 3.5: Open gripper before going down
            # self._send_feedback(f"🤏 Opening gripper above {from_square}")
            # if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
            #                                       f"Opening gripper above {from_square}", 
            #                                       gripper_state=self.GRIPPER_OPEN):
            #     return f"Failed to open gripper above {from_square}", PrimitiveResult.FAILURE
                
            # # STEP 4: Move down to pick up piece
            # self._send_feedback(f"4️⃣ Moving down to pick up piece at {from_square}")
            # if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.PICK_HEIGHT,
            #                                       f"Moving down to {from_square}", 
            #                                       gripper_state=self.GRIPPER_OPEN):
            #     return f"Failed to move down to {from_square}", PrimitiveResult.FAILURE
                
            # # STEP 4.5: Close gripper to grab piece
            # self._send_feedback(f"✊ Closing gripper to grab piece at {from_square}")
            # if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.PICK_HEIGHT,
            #                                       f"Grabbing piece at {from_square}", 
            #                                       gripper_state=self.GRIPPER_CLOSED):
            #     return f"Failed to grab piece at {from_square}", PrimitiveResult.FAILURE
                
            # # STEP 5: Move up with piece
            # self._send_feedback(f"5️⃣ Moving up with piece from {from_square}")
            # if not self._move_to_position_with_ik(from_coords['x'], from_coords['y'], self.SAFE_HEIGHT,
            #                                       f"Lifting piece from {from_square}", 
            #                                       gripper_state=self.GRIPPER_CLOSED):
            #     return f"Failed to lift piece from {from_square}", PrimitiveResult.FAILURE
                
            # # STEP 6: Move to 'to' coordinates at safe height
            # self._send_feedback(f"6️⃣ Moving to {to_square} at safe height")
            # if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
            #                                       f"Moving above {to_square}", 
            #                                       gripper_state=self.GRIPPER_CLOSED):
            #     return f"Failed to move above {to_square}", PrimitiveResult.FAILURE
                
            # # STEP 7: Move down to place piece
            # self.logger.info(f"7️⃣ Moving down to place piece at {to_square}")
            # if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.PICK_HEIGHT,
            #                                       f"Moving down to {to_square}", 
            #                                       gripper_state=self.GRIPPER_CLOSED):
            #     return f"Failed to move down to {to_square}", PrimitiveResult.FAILURE
                
            # # STEP 7.5: Open gripper to release piece
            # self._send_feedback(f"🤏 Opening gripper to release piece at {to_square}")
            # if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.PICK_HEIGHT,
            #                                       f"Releasing piece at {to_square}", 
            #                                       gripper_state=self.GRIPPER_OPEN):
            #     return f"Failed to release piece at {to_square}", PrimitiveResult.FAILURE
                
            # # STEP 8: Move up after placing
            # self._send_feedback(f"8️⃣ Moving up after placing piece at {to_square}")
            # if not self._move_to_position_with_ik(to_coords['x'], to_coords['y'], self.SAFE_HEIGHT,
            #                                       f"Lifting from {to_square}", 
            #                                       gripper_state=self.GRIPPER_OPEN):
            #     return f"Failed to lift from {to_square}", PrimitiveResult.FAILURE
                
            # # STEP 9: Move back to rest position at safe height
            # self._send_feedback("9️⃣ Moving back to rest position at safe height")
            # if not self._move_to_position_with_ik(self.REST_POSITION['x'], self.REST_POSITION['y'], self.SAFE_HEIGHT,
            #                                       "Moving to rest position at safe height"):
            #     return f"Failed to move to rest position at safe height", PrimitiveResult.FAILURE
                
            # # STEP 10: Final move to rest position
            # self._send_feedback("🔟 Moving to final rest position")
            # if not self._move_to_rest():
            #     return f"Failed to move to final rest position", PrimitiveResult.FAILURE
            
            # FINAL STEP: Capture image AFTER the move is complete
            self._send_feedback("📸 Capturing image after move")
            after_image_path = self._capture_after_image()
            if not after_image_path:
                self.logger.error("❌ Failed to capture after-move image")
                # Don't fail the whole primitive, but log a severe warning
                self._send_feedback("⚠️ WARNING: Failed to capture image after move.")
            
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

        finally:
            # Clean up camera if it's open
            if self.camera is not None:
                try:
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                    self.logger.debug("Camera released in execute() finally block")
                except Exception as e:
                    self.logger.warning(f"Error releasing camera in finally block: {e}")

    def cancel(self):
        """Cancel the chess move operation."""
        self.logger.info("Canceling chess move operation")
        
        # Release camera if it's open
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()
                self.camera = None
                self.logger.info("📹 Camera released")
            except Exception as e:
                self.logger.warning(f"Error releasing camera: {e}")
        
        return "Chess move operation canceled"

    def __del__(self):
        """Clean up camera on destruction."""
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()
                self.logger.info("Camera released in destructor")
            except Exception as e:
                self.logger.warning(f"Error releasing camera in destructor: {e}") 