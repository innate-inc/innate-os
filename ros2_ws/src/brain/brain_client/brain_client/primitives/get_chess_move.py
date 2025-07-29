#!/usr/bin/env python3
import base64
import cv2
import numpy as np
import tempfile
import os
import time
import chess
import chess.engine
import json
import requests
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from brain_client.utils.chess.update_board_state import create_annotated_board_images
from brain_client.primitives.calibrate_chess import CalibrateChess
from brain_client.utils.camera_utils import initialize_camera

class GetChessMove(Primitive):
    """
    Primitive for getting the next chess move using vision and Stockfish engine.
    Maintains persistent board state and uses calibrated chessboard corners.
    Uses Gemini 2.5 Pro for move detection.
    """

    def __init__(self, logger):
        super().__init__(logger)
        
        # Persistent state variables
        self.board_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # Starting position
        self.board_corners = None  # Will be loaded from calibration
        
        # Stockfish configuration
        self.stockfish_elo = 1500
        self.stockfish_engine = None
        
        # Camera configuration
        self.camera_index = None  # Will be determined automatically
        self.camera = None
        self.preferred_backend = cv2.CAP_V4L2  # Use V4L2 backend for better compatibility
        
        # Gemini API configuration
        self.gemini_api_key = os.getenv('GEMINI_API_KEY')
        if not self.gemini_api_key:
            self.logger.warning("⚠️  GEMINI_API_KEY environment variable not set")
        
        # Path to signal a board reset from calibrate_chess
        self.reset_signal_path = "/tmp/reset_chess_game.signal"
        
        # Store path to the "before" image, which is the last "after" image
        self.last_after_image_path = None
        self._initialize_board_state()
        
        self.logger.info("GetChessMove primitive initialized")
        self.logger.info(f"Initial board FEN: {self.board_fen}")
        self.logger.info(f"Stockfish ELO: {self.stockfish_elo}")
        if self.gemini_api_key:
            self.logger.info("✅ Gemini API key loaded")
        else:
            self.logger.warning("⚠️  Gemini API key not found")

    def _initialize_board_state(self):
        """
        Initialize or reset the board state.
        If a reset signal is found, it resets the FEN to the starting position.
        It also ensures an initial board image exists.
        """
        # Check for reset signal from calibration
        if os.path.exists(self.reset_signal_path):
            self.logger.info("🚩 Reset signal found. Resetting chess game state.")
            self.board_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            
            # Clean up the signal file so it doesn't trigger again
            try:
                os.unlink(self.reset_signal_path)
            except Exception as e:
                self.logger.warning(f"Could not remove reset signal file: {e}")

        try:
            # Check for an initial board state image
            initial_image_path = "/tmp/initial_board_setup.jpg"
            if not os.path.exists(initial_image_path):
                self.logger.info("📸 No initial board image found. Capturing one now...")
                
                # Use the utility to get a camera
                self.camera, self.camera_index = initialize_camera(self.logger, self.camera_index, self.preferred_backend)
                
                if self.camera:
                    # Capture frame
                    ret, frame = self.camera.read()
                    
                    if ret and frame is not None:
                        cv2.imwrite(initial_image_path, frame)
                        self.logger.info(f"✅ Saved initial board image to: {initial_image_path}")
                        self.last_after_image_path = initial_image_path
                    else:
                        self.logger.error("❌ Failed to capture initial board image")
                    
                    # Release camera
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                else:
                    self.logger.error("❌ Failed to initialize camera for initial setup")
            else:
                self.logger.info(f"📷 Using existing initial board image: {initial_image_path}")
                self.last_after_image_path = initial_image_path
        
        except Exception as e:
            self.logger.error(f"❌ Error initializing board state: {e}")

    @property
    def name(self):
        return "get_chess_move"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """This primitive no longer needs robot state since it captures directly from webcam."""
        return []

    def update_robot_state(self, **kwargs):
        """No longer needed since we capture directly from webcam."""
        pass

    def _initialize_camera(self):
        """Initialize the camera for capturing images using the utility function."""
        if self.camera is not None and self.camera.isOpened():
            return True  # Already initialized
            
        self.camera, self.camera_index = initialize_camera(self.logger, self.camera_index, self.preferred_backend)
        return self.camera is not None

    def _capture_image(self):
        """Capture an image from the webcam and save it to a temporary file."""
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
                self.logger.error("❌ Failed to capture frame from camera")
                return None
            
            # Create temporary file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='chess_current_')
            
            # Save the captured frame
            success = cv2.imwrite(temp_path, frame)
            os.close(temp_fd)  # Close the file descriptor
            
            if not success:
                self.logger.error("❌ Failed to save captured image")
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                return None
            
            self.logger.info(f"📸 Captured image from webcam and saved to: {temp_path}")
            return temp_path
            
        except Exception as e:
            self.logger.error(f"❌ Error capturing image: {e}")
            # Make sure camera is released even on error
            if self.camera is not None:
                try:
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                except:
                    pass
            return None

    def _encode_image_to_base64(self, image_path):
        """Encode image to base64 for Gemini API."""
        try:
            with open(image_path, 'rb') as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
            return encoded_image
        except Exception as e:
            self.logger.error(f"Error encoding image to base64: {e}")
            return None

    def _call_gemini_api(self, before_image_b64, after_image_b64):
        """Call Gemini 2.5 Pro API to analyze chess move."""
        if not self.gemini_api_key:
            self.logger.error("❌ Gemini API key not set")
            return None
        
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={self.gemini_api_key}"
            
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "text": "You are an expert chess analyst. You are going to be given annotated images of a chess game, one before and one after a move. After analyzing both images, you must just answer the move (eg e2 to e4). Think deeply each time. Every move is legal, and there are no discrepancy. If you think there is a discrepancy, it is that you made a wrong observation. Look carefully at the annotations on each square to identify which pieces moved. The images show a 10x10 grid with the chessboard occupying the center 8x8 area. Only focus on the labeled squares (a1-h8)."
                        },
                        {
                            "text": "Before move:"
                        },
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": before_image_b64
                            }
                        },
                        {
                            "text": "After move:"
                        },
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": after_image_b64
                            }
                        }
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "topK": 1,
                    "topP": 1,
                    "maxOutputTokens": 50
                }
            }
            
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            
            if 'candidates' in result and len(result['candidates']) > 0:
                text = result['candidates'][0]['content']['parts'][0]['text'].strip()
                self.logger.info(f"🤖 Gemini response: {text}")
                return text
            else:
                self.logger.error("❌ No valid response from Gemini API")
                return None
                
        except Exception as e:
            self.logger.error(f"❌ Error calling Gemini API: {e}")
            return None

    def _parse_move_from_gemini(self, gemini_response):
        """Parse chess move from Gemini response."""
        try:
            # Clean up the response
            response = gemini_response.lower().strip()
            
            # Look for patterns like "e2 to e4", "e2-e4", "e2e4"
            import re
            
            # Pattern for "square to square" format
            pattern1 = r'([a-h][1-8])\s*(?:to|->|-)\s*([a-h][1-8])'
            match1 = re.search(pattern1, response)
            if match1:
                return f"{match1.group(1)} to {match1.group(2)}"
            
            # Pattern for "square square" format
            pattern2 = r'([a-h][1-8])\s+([a-h][1-8])'
            match2 = re.search(pattern2, response)
            if match2:
                return f"{match2.group(1)} to {match2.group(2)}"
            
            # Pattern for concatenated format like "e2e4"
            pattern3 = r'([a-h][1-8])([a-h][1-8])'
            match3 = re.search(pattern3, response)
            if match3:
                return f"{match3.group(1)} to {match3.group(2)}"
            
            self.logger.error(f"❌ Could not parse move from Gemini response: {gemini_response}")
            return None
            
        except Exception as e:
            self.logger.error(f"❌ Error parsing Gemini response: {e}")
            return None

    def _update_fen_with_move(self, move_str):
        """Update the FEN string by applying the detected move."""
        try:
            # Parse the move string
            parts = move_str.strip().lower().split()
            if len(parts) == 3 and parts[1] == 'to':
                from_square = parts[0]
                to_square = parts[2]
            elif len(parts) == 2:
                from_square = parts[0]
                to_square = parts[1]
            else:
                raise ValueError(f"Invalid move format: {move_str}")
            
            # Create board from current FEN
            board = chess.Board(self.board_fen)
            
            # Convert algebraic notation to chess.Move
            from_sq = chess.parse_square(from_square)
            to_sq = chess.parse_square(to_square)
            
            # Create the move
            move = chess.Move(from_sq, to_sq)
            
            # Check if it's a promotion move (pawn reaching the end)
            piece = board.piece_at(from_sq)
            if piece and piece.piece_type == chess.PAWN:
                if (piece.color == chess.WHITE and chess.square_rank(to_sq) == 7) or \
                   (piece.color == chess.BLACK and chess.square_rank(to_sq) == 0):
                    # Default to queen promotion
                    move = chess.Move(from_sq, to_sq, promotion=chess.QUEEN)
            
            # Verify the move is legal
            if move not in board.legal_moves:
                # Try to find a legal move that matches
                for legal_move in board.legal_moves:
                    if legal_move.from_square == from_sq and legal_move.to_square == to_sq:
                        move = legal_move
                        break
                else:
                    raise ValueError(f"Move {move_str} is not legal in current position")
            
            # Apply the move
            board.push(move)
            
            # Update our FEN
            old_fen = self.board_fen
            self.board_fen = board.fen()
            
            self.logger.info(f"📋 Board state updated!")
            self.logger.info(f"Old FEN: {old_fen}")
            self.logger.info(f"New FEN: {self.board_fen}")
            self.logger.info(f"Applied move: {move}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error updating FEN with move {move_str}: {e}")
            return False

    def guidelines(self):
        return (
            "Use this to get the opponent's chess move and calculate your best response. "
            "The primitive will capture the current board state using the webcam, analyze "
            "the move made by comparing it to the last known state using Gemini, update "
            "the internal board representation, and calculate the best move using Stockfish."
        )

    def _initialize_stockfish(self):
        """Initialize Stockfish engine if not already done."""
        if self.stockfish_engine is None:
            try:
                # Try to find Stockfish in common locations
                stockfish_paths = [
                    "/usr/games/stockfish",  # Ubuntu/Debian location
                    "/usr/bin/stockfish",
                    "/usr/local/bin/stockfish",
                    "/opt/homebrew/bin/stockfish",
                    "stockfish"  # If in PATH
                ]
                
                engine_path = None
                for path in stockfish_paths:
                    if os.path.exists(path) or path == "stockfish":
                        engine_path = path
                        break
                
                if engine_path is None:
                    raise FileNotFoundError("Stockfish not found in common locations")
                
                self.stockfish_engine = chess.engine.SimpleEngine.popen_uci(engine_path)
                
                # Configure engine to play at specified ELO
                self.stockfish_engine.configure({"UCI_LimitStrength": True, "UCI_Elo": self.stockfish_elo})
                
                self.logger.info(f"Stockfish engine initialized at ELO {self.stockfish_elo}")
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to initialize Stockfish: {e}")
                return False
        return True

    def _get_best_move(self):
        """Get the best move from Stockfish."""
        try:
            # Initialize Stockfish if needed (only when we actually need to calculate a move)
            if not self._initialize_stockfish():
                self.logger.error("❌ Failed to initialize Stockfish engine")
                return None, None
            
            self.logger.info("🤖 Calculating best move with Stockfish")
            
            # Create chess board from current FEN
            board = chess.Board(self.board_fen)
            self.logger.info(f"Board to analyze:\n{board}")
            
            # Check if it's our turn (assuming we're white for now)
            if not board.turn:
                self.logger.warning("⚠️  It's Black's turn, but calculating move anyway")
            
            # Get best move from Stockfish
            result = self.stockfish_engine.play(board, chess.engine.Limit(depth=10))
            best_move = result.move
            
            if best_move is None:
                self.logger.error("❌ Stockfish returned no move")
                return None, None
            
            # Get evaluation
            info = self.stockfish_engine.analyse(board, chess.engine.Limit(depth=10))
            evaluation = info.get("score", chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE))
            
            # Format move as "from to" (e.g., "e2 to e4")
            move_str = f"{chess.square_name(best_move.from_square)} to {chess.square_name(best_move.to_square)}"
            
            # Format evaluation
            if evaluation.is_mate():
                eval_str = f"M{evaluation.mate()}"
            else:
                eval_str = f"{evaluation.pov(chess.WHITE).score() / 100:.2f}"
                if evaluation.pov(chess.WHITE).score() > 0:
                    eval_str = f"+{eval_str}"
            
            self.logger.info(f"✅ Best move: {move_str}")
            self.logger.info(f"📊 Evaluation: {eval_str}")
            
            return move_str, eval_str
            
        except Exception as e:
            self.logger.error(f"Error calculating best move: {e}")
            return None, None

    def execute(self, **kwargs):
        """
        Execute the chess move calculation pipeline using webcam capture and Gemini analysis.
        
        Returns:
            tuple: (result_dict, result_status) where result_dict contains:
                   {"move": "e2 to e4", "evaluation": "+0.3", "board_updated": True}
        """
        self.logger.info("🚀 Starting GetChessMove execution with Gemini-based move detection")
        
        # Check for and apply reset signal if a game is in progress
        self._initialize_board_state()
        
        # Check if the system is calibrated
        if not CalibrateChess.is_calibrated():
            error_msg = "Chess vision system is not calibrated. Please run 'calibrate_chess' first."
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE
        
        # Load calibrated corners
        self.board_corners = CalibrateChess.load_calibrated_corners(self.logger)
        if self.board_corners is None:
            error_msg = "Failed to load calibrated corners."
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE
        
        after_image_path = None
        before_image_path = self.last_after_image_path
        temp_before_annotated = None
        temp_after_annotated = None
        
        try:
            # Step 1: Get the "before" image (which is the last "after" image)
            if not before_image_path or not os.path.exists(before_image_path):
                error_msg = "No previous board state image available to compare against."
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 2: Capture the current ("after") image
            after_image_path = self._capture_image()
            if not after_image_path:
                error_msg = "Failed to capture current board image from webcam"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 3: Create annotated images for both before and after states
            self.logger.info("🖼️  Creating annotated board images using calibrated corners")
            before_annotated, after_annotated, corners = create_annotated_board_images(
                before_image_path, after_image_path, 
                self.logger,
                corners=self.board_corners, 
                save_debug_images=True
            )
            
            if before_annotated is None or after_annotated is None:
                error_msg = "Failed to create annotated board images"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Save annotated images to temporary files for Gemini
            temp_before_fd, temp_before_annotated = tempfile.mkstemp(suffix='.jpg', prefix='annotated_before_')
            temp_after_fd, temp_after_annotated = tempfile.mkstemp(suffix='.jpg', prefix='annotated_after_')
            
            cv2.imwrite(temp_before_annotated, before_annotated)
            cv2.imwrite(temp_after_annotated, after_annotated)
            os.close(temp_before_fd)
            os.close(temp_after_fd)

            # Step 4: Encode images to base64 for Gemini
            self.logger.info("🔄 Encoding images for Gemini API")
            before_b64 = self._encode_image_to_base64(temp_before_annotated)
            after_b64 = self._encode_image_to_base64(temp_after_annotated)
            
            if not before_b64 or not after_b64:
                error_msg = "Failed to encode images for Gemini API"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 5: Call Gemini API to detect the move
            self.logger.info("🤖 Calling Gemini API to analyze move")
            gemini_response = self._call_gemini_api(before_b64, after_b64)
            
            if not gemini_response:
                error_msg = "Failed to get response from Gemini API"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 6: Parse the move from Gemini response
            detected_move_str = self._parse_move_from_gemini(gemini_response)
            
            if not detected_move_str:
                error_msg = f"Failed to parse move from Gemini response: {gemini_response}"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 7: Update FEN with the detected move
            self.logger.info(f"🔄 Updating board state with move: {detected_move_str}")
            board_updated = self._update_fen_with_move(detected_move_str)
            
            if not board_updated:
                error_msg = f"Failed to update board state with move: {detected_move_str}"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 8: Get best move from Stockfish
            best_move, evaluation = self._get_best_move()
            
            if best_move is None:
                error_msg = "Failed to calculate best move"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 9: Update the 'before' image for the next turn
            self.last_after_image_path = after_image_path
            
            # Prepare result
            result = {
                "move": best_move,
                "evaluation": evaluation,
                "board_updated": board_updated,
                "detected_move": detected_move_str
            }
            
            self.logger.info("✅ GetChessMove completed successfully")
            self.logger.info(f"📤 Result: {result}")
            
            # Convert result to JSON string for ROS2 action compatibility
            result_message = json.dumps(result)
            
            return result_message, PrimitiveResult.SUCCESS

        except Exception as e:
            error_msg = f"Unexpected error during chess move calculation: {e}"
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE

        finally:
            # Clean up camera first
            if self.camera is not None:
                try:
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                    self.logger.debug("Camera released in execute() finally block")
                except Exception as e:
                    self.logger.warning(f"Error releasing camera in finally block: {e}")
            
            # Clean up temporary files (don't delete the new after_image_path)
            temp_files = [temp_before_annotated, temp_after_annotated]
            for temp_file in temp_files:
                try:
                    if temp_file and os.path.exists(temp_file):
                        os.unlink(temp_file)
                        self.logger.debug(f"Cleaned up temporary file: {temp_file}")
                except Exception as e:
                    self.logger.warning(f"Failed to clean up temporary file {temp_file}: {e}")

    def cancel(self):
        """Cancel the chess move calculation and release camera."""
        self.logger.info("🛑 Canceling chess move calculation")
        
        # Release camera if it's open
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()  # Clean up any OpenCV windows
                self.camera = None
                self.logger.info("📹 Camera released")
            except Exception as e:
                self.logger.warning(f"Error releasing camera: {e}")
        
        return "Chess move calculation canceled"

    def __del__(self):
        """Clean up Stockfish engine and camera on destruction."""
        if self.stockfish_engine:
            try:
                self.stockfish_engine.quit()
                self.logger.info("Stockfish engine cleaned up")
            except Exception as e:
                self.logger.warning(f"Error cleaning up Stockfish engine: {e}")
        
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()  # Clean up any OpenCV windows
                self.logger.info("Camera released in destructor")
            except Exception as e:
                self.logger.warning(f"Error releasing camera in destructor: {e}") 