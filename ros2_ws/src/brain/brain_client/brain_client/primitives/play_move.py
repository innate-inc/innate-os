#!/usr/bin/env python3
import time
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from brain_client.primitives.types import Primitive, PrimitiveResult
import threading
import re


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

    # Rest position (example - adjust based on your robot's home position)
    REST_POSITION = {'x': 0.3, 'y': 0.0, 'z': 0.2}

    def __init__(self, logger):
        super().__init__(logger)
        self.ik_delta_publisher = None
        self.fk_pose_subscriber = None
        self.current_pose = None
        self.pose_received = threading.Event()

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

    def _wait_for_current_pose(self, timeout=5.0):
        """Wait for the current FK pose to be received."""
        if not self.fk_pose_subscriber:
            self.fk_pose_subscriber = self.node.create_subscription(
                PoseStamped,
                'fk_pose',
                self._fk_pose_callback,
                10
            )
        
        self.pose_received.clear()
        if self.pose_received.wait(timeout):
            return True
        else:
            self.logger.error("Timeout waiting for current pose")
            return False

    def _fk_pose_callback(self, msg: PoseStamped):
        """Callback for FK pose updates."""
        self.current_pose = {
            'x': msg.pose.position.x,
            'y': msg.pose.position.y,
            'z': msg.pose.position.z
        }
        self.pose_received.set()

    def _move_to_position(self, target_position, description=""):
        """
        Move the arm to a target position using inverse kinematics.
        target_position should be a dict with 'x', 'y', 'z' keys.
        """
        if not self.current_pose:
            self.logger.error("Current pose not available")
            return False

        # Calculate delta from current position to target
        delta_x = target_position['x'] - self.current_pose['x']
        delta_y = target_position['y'] - self.current_pose['y']
        delta_z = target_position['z'] - self.current_pose['z']

        # Create Twist message for IK delta
        twist_msg = Twist()
        twist_msg.linear.x = delta_x
        twist_msg.linear.y = delta_y
        twist_msg.linear.z = delta_z
        twist_msg.angular.x = 0.0
        twist_msg.angular.y = 0.0
        twist_msg.angular.z = 0.0

        self.logger.info(f"{description} - Moving to position: x={target_position['x']:.4f}, y={target_position['y']:.4f}, z={target_position['z']:.4f}")
        self.logger.info(f"Delta: dx={delta_x:.4f}, dy={delta_y:.4f}, dz={delta_z:.4f}")

        # Publish the IK delta command
        if not self.ik_delta_publisher:
            self.ik_delta_publisher = self.node.create_publisher(
                Twist,
                'ik_delta',
                10
            )

        self.ik_delta_publisher.publish(twist_msg)

        # Wait a moment for the IK to process and update current pose
        time.sleep(1.0)
        
        # Get updated pose
        if self._wait_for_current_pose():
            return True
        else:
            return False

    def execute(self, move_string=""):
        """
        Execute the chess move.
        
        Args:
            move_string: Chess move in notation like 'e2 to e4'
        """
        if not self.node:
            self.logger.error("PlayMove primitive is not functional due to missing ROS node.")
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not move_string:
            return "No move specified", PrimitiveResult.FAILURE

        # Parse the move
        from_square, to_square = self._parse_move(move_string)
        if not from_square or not to_square:
            return f"Invalid move format: '{move_string}'. Use format like 'e2 to e4'", PrimitiveResult.FAILURE

        self.logger.info(f"\033[96m[BrainClient] Initiating chess move: {from_square} to {to_square}\033[0m")

        # Get current pose first
        if not self._wait_for_current_pose():
            return "Failed to get current arm pose", PrimitiveResult.FAILURE

        try:
            # Step 1: Move above the 'from' square at z=0.15
            from_coords = self.CHESS_COORDINATES[from_square].copy()
            from_coords['z'] = 0.15
            
            self._send_feedback(f"Moving above {from_square} square...")
            if not self._move_to_position(from_coords, f"Step 1: Above {from_square}"):
                return f"Failed to move above {from_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 2: Lower to z=0.05 to pick up the piece
            from_coords['z'] = 0.05
            
            self._send_feedback(f"Lowering to pick up piece from {from_square}...")
            if not self._move_to_position(from_coords, f"Step 2: Pick up from {from_square}"):
                return f"Failed to lower to {from_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 3: Raise back to z=0.15
            from_coords['z'] = 0.15
            
            self._send_feedback("Lifting piece...")
            if not self._move_to_position(from_coords, f"Step 3: Lift from {from_square}"):
                return f"Failed to lift from {from_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 4: Move above the 'to' square at z=0.15
            to_coords = self.CHESS_COORDINATES[to_square].copy()
            to_coords['z'] = 0.15
            
            self._send_feedback(f"Moving to {to_square} square...")
            if not self._move_to_position(to_coords, f"Step 4: Above {to_square}"):
                return f"Failed to move above {to_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 5: Lower to z=0.05 to place the piece
            to_coords['z'] = 0.05
            
            self._send_feedback(f"Placing piece on {to_square}...")
            if not self._move_to_position(to_coords, f"Step 5: Place on {to_square}"):
                return f"Failed to lower to {to_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 6: Raise back to z=0.15
            to_coords['z'] = 0.15
            
            self._send_feedback("Releasing piece...")
            if not self._move_to_position(to_coords, f"Step 6: Release at {to_square}"):
                return f"Failed to lift from {to_square} square", PrimitiveResult.FAILURE

            time.sleep(1.0)

            # Step 7: Return to rest position
            self._send_feedback("Returning to rest position...")
            if not self._move_to_position(self.REST_POSITION, "Step 7: Return to rest"):
                return "Failed to return to rest position", PrimitiveResult.FAILURE

            time.sleep(1.0)

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