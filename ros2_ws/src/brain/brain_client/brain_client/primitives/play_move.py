#!/usr/bin/env python3
import rclpy
import time
import threading
from geometry_msgs.msg import Twist
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray
from maurice_msgs.srv import GotoJS
from brain_client.primitives.types import Primitive, PrimitiveResult


class PlayMove(Primitive):
    """
    Primitive for playing a chess move by moving the robot arm from one square to another.
    Uses inverse kinematics to calculate joint positions for chess board coordinates.
    """

    # Chess board coordinates mapping to 3D positions
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
        self.goto_js_client = None
        self.latest_ik_solution = None

    @property
    def name(self):
        return "play_move"

    def guidelines(self):
        return (
            "Use this to play a chess move by moving a piece from one square to another. "
            "Specify the move in format like 'e2 to e4' or 'a1 to a3'. "
            "The robot will move its arm to pick up the piece from the source square "
            "and place it on the destination square."
        )

    def guidelines_when_running(self):
        return (
            "The robot is executing a chess move. Watch the arm camera to see "
            "if the piece is being picked up and placed correctly. "
            "The move involves going to the source square, picking up the piece, "
            "and then moving to the destination square to place it."
        )

    def _parse_move(self, move_string):
        """
        Parse a move string like 'e2 to e4' into source and destination squares.
        Returns (from_square, to_square) or (None, None) if invalid.
        """
        try:
            # Handle different formats: "e2 to e4", "e2-e4", "e2 e4"
            move_string = move_string.lower().strip()
            
            if " to " in move_string:
                parts = move_string.split(" to ")
            elif "-" in move_string:
                parts = move_string.split("-")
            elif " " in move_string:
                parts = move_string.split()
            else:
                return None, None
            
            if len(parts) != 2:
                return None, None
            
            from_square = parts[0].strip()
            to_square = parts[1].strip()
            
            # Validate squares exist in our coordinate mapping
            if from_square not in self.CHESS_COORDINATES or to_square not in self.CHESS_COORDINATES:
                self.logger.error(f"Invalid chess squares: {from_square}, {to_square}")
                return None, None
            
            return from_square, to_square
        
        except Exception as e:
            self.logger.error(f"Error parsing move '{move_string}': {e}")
            return None, None

    def _solve_ik_for_position(self, target_x, target_y, target_z, target_roll=0.0, target_pitch=1.396, target_yaw=0.0, timeout=10.0):
        """
        Solve IK for a target position using the IK delta topic.
        Returns True if successful, False otherwise.
        """
        if not self.node:
            self.logger.error("No ROS node available for IK solving")
            return False

        ik_delta_publisher = None
        ik_solution_subscriber = None
        
        try:
            # Create publishers and subscribers
            ik_delta_publisher = self.node.create_publisher(Twist, 'ik_delta', 10)
            
            # Local state for this IK solve
            latest_ik_solution = None
            ik_solution_received = threading.Event()
            
            def ik_solution_callback(msg: JointState):
                nonlocal latest_ik_solution, ik_solution_received
                if msg and len(msg.position) > 0:
                    latest_ik_solution = msg
                    ik_solution_received.set()
                    self.logger.info(f"Received IK solution: {[f'{pos:.3f}' for pos in msg.position]}")
                else:
                    self.logger.warn("Received invalid IK solution message")
            
            ik_solution_subscriber = self.node.create_subscription(
                JointState,
                'ik_solution',
                ik_solution_callback,
                10
            )

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

            # Publish the IK request
            ik_delta_publisher.publish(twist_msg)
            
            # Small delay to ensure message is sent
            time.sleep(0.1)

            # Wait for IK solution
            start_time = time.time()
            while (time.time() - start_time) < timeout:
                rclpy.spin_once(self.node, timeout_sec=0.1)
                
                if ik_solution_received.is_set():
                    self.logger.info(f"IK solution received successfully after {time.time() - start_time:.2f}s")
                    self.latest_ik_solution = latest_ik_solution
                    return True
                
                time.sleep(0.05)
            
            self.logger.error("IK solution not received within timeout")
            return False
            
        except Exception as e:
            self.logger.error(f"Error solving IK: {e}")
            return False
        finally:
            # Cleanup
            if ik_delta_publisher:
                self.node.destroy_publisher(ik_delta_publisher)
            if ik_solution_subscriber:
                self.node.destroy_subscription(ik_solution_subscriber)

    def _execute_trajectory_to_ik_solution(self, trajectory_time=3):
        """Execute trajectory to the latest IK solution using GotoJS service"""
        if not self.latest_ik_solution:
            self.logger.error("No IK solution available")
            return False

        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(GotoJS, '/maurice_arm/goto_js')
            
        if not self.goto_js_client.wait_for_service(timeout_sec=5.0):
            self.logger.error("GotoJS service not available")
            return False

        # Create service request
        request = GotoJS.Request()
        request.data = Float64MultiArray()

        # IK returns joint positions, append 0.0 for the 6th joint if needed
        ik_positions = list(self.latest_ik_solution.position)
        if len(ik_positions) == 5:
            ik_positions.append(0.0)  # Add 6th joint position as 0.0

        request.data.data = ik_positions
        request.time = trajectory_time

        self.logger.info(f"Executing trajectory to joint positions: {[f'{pos:.3f}' for pos in request.data.data]}")

        # Call service
        future = self.goto_js_client.call_async(request)
        
        try:
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=trajectory_time + 5.0)
        except Exception as e:
            self.logger.error(f"Exception during service call: {e}")
            return False

        if future.result() is not None:
            response = future.result()
            self.logger.info(f"GotoJS service responded: {response.success}")
            return response.success
        else:
            self.logger.error("GotoJS service call timed out")
            return False

    def _move_to_position(self, target_position, description=""):
        """
        Move the arm to a target position using inverse kinematics.
        target_position should be a dict with 'x', 'y', 'z' keys.
        """
        target_x = target_position['x']
        target_y = target_position['y']
        target_z = target_position['z'] + 0.05  # Add 5cm above the square for safety

        self.logger.info(f"{description} - Moving to position: x={target_x:.4f}, y={target_y:.4f}, z={target_z:.4f}")
        self._send_feedback(f"{description} - Moving to chess square")

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
        self.logger.info(f"PlayMove.execute() called with move_string='{move_string}'")
        
        # Reset any persistent state
        self.latest_ik_solution = None
        
        if not self.node:
            self.logger.error("PlayMove primitive is not functional due to missing ROS node.")
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not move_string:
            self.logger.error("No move specified")
            return "No move specified", PrimitiveResult.FAILURE

        # Parse the move
        from_square, to_square = self._parse_move(move_string)
        if not from_square or not to_square:
            return f"Invalid move format: '{move_string}'. Use format like 'e2 to e4'", PrimitiveResult.FAILURE

        self.logger.info(f"\033[96m[BrainClient] Initiating chess move: {from_square} to {to_square}\033[0m")

        try:
            # Get positions for both squares
            from_position = self.CHESS_COORDINATES[from_square]
            to_position = self.CHESS_COORDINATES[to_square]

            # Step 1: Move to source square
            self._send_feedback(f"Moving to source square {from_square}")
            if not self._move_to_position(from_position, f"Source square {from_square}"):
                return f"Failed to move to source square {from_square}", PrimitiveResult.FAILURE

            time.sleep(1.0)  # Brief pause

            # Step 2: Move to destination square
            self._send_feedback(f"Moving to destination square {to_square}")
            if not self._move_to_position(to_position, f"Destination square {to_square}"):
                return f"Failed to move to destination square {to_square}", PrimitiveResult.FAILURE

            self.logger.info(f"\033[92m[BrainClient] Chess move completed: {from_square} to {to_square}\033[0m")
            self._send_feedback(f"Chess move completed: {from_square} to {to_square}")
            
            return f"Chess move completed: {from_square} to {to_square}", PrimitiveResult.SUCCESS

        except Exception as e:
            self.logger.error(f"Error executing chess move: {e}")
            return f"Error executing chess move: {e}", PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancel the chess move operation.
        """
        self.logger.info("\033[91m[BrainClient] Chess move operation cancellation requested.\033[0m")
        return "Chess move operation cancelled" 