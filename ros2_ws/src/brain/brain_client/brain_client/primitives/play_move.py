#!/usr/bin/env python3
import rclpy
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from maurice_msgs.srv import GotoJS
from std_msgs.msg import Float64MultiArray
import time
import cv2
import os
from brain_client.utils.camera_utils import initialize_camera
import chess
import json
import pkg_resources

def _load_fen_from_file(logger):
    """Load FEN from the shared file."""
    fen_file_path = "/tmp/chess_game_fen.txt"
    try:
        if os.path.exists(fen_file_path):
            with open(fen_file_path, 'r') as f:
                return f.read().strip()
        else:
            logger.warning(f"FEN file not found at {fen_file_path}. Using default.")
            return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    except Exception as e:
        logger.error(f"Error loading FEN from file: {e}")
        return None

def _save_fen_to_file(fen, logger):
    """Save FEN to the shared file."""
    fen_file_path = "/tmp/chess_game_fen.txt"
    try:
        with open(fen_file_path, 'w') as f:
            f.write(fen)
        logger.info(f"Saved new FEN to {fen_file_path}")
    except Exception as e:
        logger.error(f"Error saving FEN to file: {e}")

def _append_to_history(move_str, new_fen, logger):
    """Append move and FEN to their respective history files."""
    move_history_path = "/tmp/chess_move_history.txt"
    fen_history_path = "/tmp/chess_fen_history.txt"
    try:
        with open(move_history_path, 'a') as f:
            f.write(move_str + '\n')
        with open(fen_history_path, 'a') as f:
            f.write(new_fen + '\n')
        logger.info(f"Appended '{move_str}' and new FEN to history files.")
    except Exception as e:
        logger.error(f"Error appending to history files: {e}")

class PlayMove(Primitive):
    """
    Primitive for playing a chess move by executing a complete pick-and-place sequence
    using pre-recorded joint positions.
    This primitive now captures an image AFTER the move is completed.
    """

    # Rest position joint angles
    FINAL_REST_JOINT_POSITIONS = [-1.5477, 1.2241, -1.5309, 1.0814, -0.0874, -0.113]
    
    # Gripper control
    GRIPPER_OPEN = 0.5
    GRIPPER_CLOSED = 0.0

    def __init__(self, logger):
        super().__init__(logger)
        self.goto_js_client = None  # Service client for trajectory execution
        self.chess_positions = self._load_chess_positions()
        
        # Camera configuration for image capture
        self.camera_index = None  # Will be determined automatically
        self.camera = None
        self.preferred_backend = cv2.CAP_V4L2  # Use V4L2 backend for better compatibility

    def _load_chess_positions(self):
        """Load chess positions from the JSON file."""
        try:
            # Correctly locate the file within the package
            file_path = pkg_resources.resource_filename('brain_client', 'utils/chess/chess_positions.json')
            with open(file_path, 'r') as f:
                data = json.load(f)
            self.logger.info(f"Successfully loaded {len(data['positions'])} chess positions.")
            return data['positions']
        except Exception as e:
            self.logger.error(f"Failed to load chess_positions.json: {e}")
            return None

    @property
    def name(self):
        return "play_move"

    def guidelines(self):
        return (
            "Use this to play a chess move. Provide the move in format 'from_square to_square' "
            "like 'a2 to a4' or 'e1 to e3'. This will use pre-recorded joint positions "
            "to move the arm. After the move, it will capture an image of the board for vision analysis."
        )

    def get_required_robot_states(self):
        """This primitive no longer requires state from the action server."""
        return []

    def update_robot_state(self, **kwargs):
        """This primitive does not use external state updates."""
        pass

    def _initialize_camera(self):
        """Initialize the camera for capturing images."""
        if self.camera is not None and self.camera.isOpened():
            return True  # Already initialized
            
        self.camera, self.camera_index = initialize_camera(self.logger, self.camera_index, self.preferred_backend)
        return self.camera is not None

    def _capture_after_image(self):
        """Capture an image after the move is completed and update the definitive board state."""
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
            
            # The definitive path for the last known board state
            board_state_path = "/tmp/last_known_board_state.jpg"
            
            # Save the captured frame, overwriting the previous state
            success = cv2.imwrite(board_state_path, frame)
            
            if not success:
                self.logger.error(f"❌ Failed to save after-move image to {board_state_path}")
                return None
            
            self.logger.info(f"📸 Captured after-move image and updated definitive state: {board_state_path}")
            return board_state_path
            
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

    def _update_fen_with_move(self, board_fen, move_str):
        """Update the FEN string by applying the specified move."""
        try:
            board = chess.Board(board_fen)
            from_square, to_square = self._parse_chess_move(move_str)
            
            from_sq = chess.parse_square(from_square)
            to_sq = chess.parse_square(to_square)
            
            move = chess.Move(from_sq, to_sq)
            
            # Handle promotions
            piece = board.piece_at(from_sq)
            if piece and piece.piece_type == chess.PAWN:
                if (piece.color == chess.WHITE and chess.square_rank(to_sq) == 7) or \
                   (piece.color == chess.BLACK and chess.square_rank(to_sq) == 0):
                    move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)

            if move in board.legal_moves:
                board.push(move)
                new_fen = board.fen()
                self.logger.info(f"Updated FEN from '{board_fen}' to '{new_fen}'")
                return new_fen
            else:
                # Find matching legal move if necessary (e.g. for castling)
                for legal_move in board.legal_moves:
                    if legal_move.from_square == from_sq and legal_move.to_square == to_sq:
                        board.push(legal_move)
                        new_fen = board.fen()
                        self.logger.info(f"Updated FEN with matching legal move: '{new_fen}'")
                        return new_fen
                raise ValueError(f"Move '{move_str}' is not legal.")

        except Exception as e:
            self.logger.error(f"Error updating FEN: {e}")
            return None

    def _execute_trajectory(self, joint_positions, trajectory_time=3, gripper_state=None):
        """Execute a trajectory to the given joint positions with optional gripper control."""
        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(GotoJS, 'maurice_arm/goto_js')
            
        if not self.goto_js_client.wait_for_service(timeout_sec=5.0):
            self.logger.error("GotoJS service not available")
            return False

        # Ensure we have 6 joint positions
        joint_positions = list(joint_positions)
        if len(joint_positions) < 6:
            self.logger.error(f"Not enough joint positions provided: {joint_positions}")
            return False
            
        # Set gripper state if specified
        if gripper_state is not None:
            joint_positions[5] = gripper_state  # Set the 6th joint (gripper)
            
        request = GotoJS.Request()
        request.data = Float64MultiArray()
        request.data.data = joint_positions
        request.time = float(trajectory_time)

        gripper_desc = ""
        if gripper_state is not None:
            gripper_desc = f" (gripper {'OPEN' if gripper_state == self.GRIPPER_OPEN else 'CLOSED'})"

        self.logger.info(f"Executing trajectory: {[f'{pos:.3f}' for pos in joint_positions]}{gripper_desc}")
        
        # Call service synchronously
        future = self.goto_js_client.call_async(request)
        
        # Wait for service response
        start_time = time.time()
        # The timeout for the service call itself
        service_timeout = trajectory_time + 5.0
        while not future.done() and (time.time() - start_time) < service_timeout:
            time.sleep(0.1)
            
        if future.done():
            response = future.result()
            if response and response.success:
                self.logger.info(f"Trajectory started successfully, waiting {trajectory_time + 1.5}s for completion")
                time.sleep(trajectory_time + 1.5)  # Wait for trajectory to complete + buffer
                return True
            else:
                self.logger.error(f"Trajectory execution failed: {response.message if response else 'No response'}")
                return False
        else:
            self.logger.error("Service call timed out")
            return False

    def _move_to_square(self, square, pose_type, gripper_state=None, trajectory_time=3):
        """Move the arm to a specific chess square's high or low pose."""
        position_name = f"{square}_{pose_type}"
        if not self.chess_positions or position_name not in self.chess_positions:
            self.logger.error(f"Position '{position_name}' not found in loaded positions.")
            return False
            
        joint_positions = self.chess_positions[position_name]['joint_positions']
        
        self._send_feedback(f"Moving to {square} ({pose_type})")
        return self._execute_trajectory(joint_positions, trajectory_time, gripper_state)

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

        if not self.chess_positions:
            return "Chess positions not loaded, cannot execute move.", PrimitiveResult.FAILURE

        if not move_str:
            self._send_feedback("❌ No chess move provided")
            return "No chess move provided", PrimitiveResult.FAILURE

        try:
            # Load the current board state from the shared file
            board_fen = _load_fen_from_file(self.logger)
            if board_fen is None:
                return "Failed to load board state from file", PrimitiveResult.FAILURE
            self.logger.info(f"Loaded board FEN for play_move: {board_fen}")

            # Update the FEN with the move we are about to play
            new_fen = self._update_fen_with_move(board_fen, move_str)
            if new_fen is None:
                return f"Failed to update board state with move {move_str}", PrimitiveResult.FAILURE
            
            # Save the new FEN back to the file
            _save_fen_to_file(new_fen, self.logger)
            # Append to history
            _append_to_history(move_str, new_fen, self.logger)

            # Parse the chess move
            self._send_feedback(f"🔍 Parsing chess move: '{move_str}'")
            from_square, to_square = self._parse_chess_move(move_str)
            self.logger.info(f"Parsed chess move: {from_square} to {to_square}")
            self._send_feedback(f"✅ Parsed chess move: {from_square} → {to_square}")

            # Validate squares
            if f"{from_square}_high" not in self.chess_positions or f"{to_square}_high" not in self.chess_positions:
                err_msg = f"Move squares '{from_square}' or '{to_square}' not in recorded positions."
                self.logger.error(err_msg)
                return err_msg, PrimitiveResult.FAILURE

            # --- MOVE SEQUENCE ---
            self._send_feedback(f"📋 Executing chess move sequence: {from_square} → {to_square}")
            
            # STEP 1: Move to a safe starting position (d2_high)
            self._send_feedback("1️⃣ Moving to safe start position (d2_high)")
            if not self._move_to_square('d2', 'high', gripper_state=self.GRIPPER_OPEN):
                 return f"Failed to move to safe start position", PrimitiveResult.FAILURE

            # STEP 2: Move to 'from' high position, gripper open
            self._send_feedback(f"2️⃣ Moving above {from_square} (gripper open)")
            if not self._move_to_square(from_square, 'high', gripper_state=self.GRIPPER_OPEN):
                return f"Failed to move above {from_square}", PrimitiveResult.FAILURE

            # STEP 3: Move to 'from' low position to pick
            self._send_feedback(f"3️⃣ Moving down to {from_square} to pick piece")
            if not self._move_to_square(from_square, 'low', gripper_state=self.GRIPPER_OPEN):
                return f"Failed to move down to {from_square}", PrimitiveResult.FAILURE

            # STEP 4: Close gripper
            self._send_feedback(f"4️⃣ Closing gripper to grab piece at {from_square}")
            if not self._execute_trajectory(self.chess_positions[f"{from_square}_low"]['joint_positions'], 
                                            trajectory_time=1.5, gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to close gripper at {from_square}", PrimitiveResult.FAILURE

            # STEP 5: Move back to 'from' high position
            self._send_feedback(f"5️⃣ Lifting piece from {from_square}")
            if not self._move_to_square(from_square, 'high', gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to lift piece from {from_square}", PrimitiveResult.FAILURE

            # STEP 6: Move to 'to' high position
            self._send_feedback(f"6️⃣ Moving to {to_square} high position")
            if not self._move_to_square(to_square, 'high', gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to move above {to_square}", PrimitiveResult.FAILURE

            # STEP 7: Move to 'to' low position to place
            self._send_feedback(f"7️⃣ Moving down to {to_square} to place piece")
            if not self._move_to_square(to_square, 'low', gripper_state=self.GRIPPER_CLOSED):
                return f"Failed to move down to {to_square}", PrimitiveResult.FAILURE

            # STEP 8: Open gripper
            self._send_feedback(f"8️⃣ Opening gripper to release piece at {to_square}")
            if not self._execute_trajectory(self.chess_positions[f"{to_square}_low"]['joint_positions'], 
                                            trajectory_time=1.5, gripper_state=self.GRIPPER_OPEN):
                return f"Failed to open gripper at {to_square}", PrimitiveResult.FAILURE

            # STEP 9: Move back to 'to' high position
            self._send_feedback(f"9️⃣ Moving up from {to_square}")
            if not self._move_to_square(to_square, 'high', gripper_state=self.GRIPPER_OPEN):
                return f"Failed to move up from {to_square}", PrimitiveResult.FAILURE
                
            # STEP 10: Move back to safe position (d2_high)
            self._send_feedback("🔟 Moving back to safe position (d2_high)")
            if not self._move_to_square('d2', 'high', gripper_state=self.GRIPPER_OPEN):
                return f"Failed to move back to safe position", PrimitiveResult.FAILURE
            
            # STEP 11: Final move to rest position
            self._send_feedback("🏠 Moving to final rest position")
            if not self._execute_trajectory(self.FINAL_REST_JOINT_POSITIONS, trajectory_time=4):
                 return f"Failed to move to final rest position", PrimitiveResult.FAILURE

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