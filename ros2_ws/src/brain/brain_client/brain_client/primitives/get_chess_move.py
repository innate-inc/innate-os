#!/usr/bin/env python3
import base64
import cv2
import numpy as np
import tempfile
import os
import chess
import chess.engine
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from brain_client.utils.chess.update_board_state import get_new_fen


class GetChessMove(Primitive):
    """
    Primitive for getting the next chess move using vision and Stockfish engine.
    Maintains persistent board state and chessboard corner detection.
    """

    def __init__(self, logger):
        super().__init__(logger)
        
        # Persistent state variables
        self.board_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"  # Starting position
        self.board_corners = None  # Will store detected corners for reuse
        
        # Physical parameters (hardcoded)
        self.chessboard_square_size = 42  # mm per square
        self.chessboard_height = 25  # mm height of board
        
        # Stockfish configuration
        self.stockfish_elo = 1000
        self.stockfish_engine = None
        
        # Camera image state
        self.last_main_camera_image_b64 = None
        
        self.logger.info("GetChessMove primitive initialized")
        self.logger.info(f"Initial board FEN: {self.board_fen}")
        self.logger.info(f"Stockfish ELO: {self.stockfish_elo}")

    @property
    def name(self):
        return "get_chess_move"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """Declare that this primitive needs the last camera image."""
        return [RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64]

    def update_robot_state(self, **kwargs):
        """Store the last image received from the robot state."""
        self.last_main_camera_image_b64 = kwargs.get(
            RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value
        )
        if self.last_main_camera_image_b64:
            self.logger.debug("[GetChessMove] Received camera image for analysis")
        else:
            self.logger.warning("[GetChessMove] No camera image received")

    def guidelines(self):
        return (
            "Use this to get the next best chess move. The primitive will automatically "
            "capture the current board state using the camera, update the internal board "
            "representation, and calculate the best move using Stockfish at ELO 1000. "
            "No arguments needed - it maintains persistent board state."
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

    def _save_image_to_temp_file(self, image_b64):
        """Save base64 image to temporary file and return path."""
        try:
            # Decode base64 image
            image_data = base64.b64decode(image_b64)
            
            # Create temporary file
            temp_fd, temp_path = tempfile.mkstemp(suffix='.jpg', prefix='chess_board_')
            
            # Write image data to file
            with os.fdopen(temp_fd, 'wb') as temp_file:
                temp_file.write(image_data)
            
            self.logger.debug(f"Saved camera image to temporary file: {temp_path}")
            return temp_path
            
        except Exception as e:
            self.logger.error(f"Failed to save image to temporary file: {e}")
            return None

    def _detect_board_corners(self, image_path):
        """Detect chessboard corners using the vision pipeline."""
        try:
            self.logger.info("🔍 Detecting chessboard corners (first time setup)")
            
            # Import the detection functions
            from brain_client.utils.chess.chess_detection import detectChessboardCorners
            
            # Detect the corners
            corners, success = detectChessboardCorners(image_path)
            
            if success and corners is not None:
                self.logger.info("✅ Successfully detected chessboard corners")
                self.logger.info(f"📍 Corner coordinates: {corners}")
                
                # Store the actual corner coordinates as a numpy array
                self.board_corners = corners
                return True
            else:
                self.logger.error("❌ Failed to detect chessboard corners")
                return False
                
        except Exception as e:
            self.logger.error(f"Error during corner detection: {e}")
            return False

    def _update_board_state(self, image_path):
        """Update board state using vision analysis."""
        try:
            self.logger.info(f"🔄 Updating board state from image")
            self.logger.info(f"Current FEN: {self.board_fen}")
            
            # Use the update_board_state function with stored corners
            new_fen, detected_move = get_new_fen(
                image_path, 
                self.board_fen, 
                confidence_threshold=0.99, 
                corners=self.board_corners
            )
            
            board_updated = (new_fen != self.board_fen)
            
            if board_updated:
                self.logger.info(f"📋 Board state updated!")
                self.logger.info(f"Old FEN: {self.board_fen}")
                self.logger.info(f"New FEN: {new_fen}")
                if detected_move:
                    self.logger.info(f"Detected move: {detected_move}")
                self.board_fen = new_fen
            else:
                self.logger.info("📋 No changes detected in board state")
            
            return board_updated, detected_move
            
        except Exception as e:
            self.logger.error(f"Error updating board state: {e}")
            return False, None

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
        Execute the chess move calculation pipeline.
        
        Returns:
            tuple: (result_dict, result_status) where result_dict contains:
                   {"move": "e2 to e4", "evaluation": "+0.3", "board_updated": True}
        """
        self.logger.info("🚀 Starting GetChessMove execution")
        
        # Check if we have a camera image
        if not self.last_main_camera_image_b64:
            error_msg = "No camera image available for analysis"
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE

        # Save image to temporary file
        temp_image_path = self._save_image_to_temp_file(self.last_main_camera_image_b64)
        if not temp_image_path:
            error_msg = "Failed to save camera image for processing"
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE

        try:
            # Step 1: Detect corners if not already done
            if self.board_corners is None:
                self.logger.info("🔧 First time setup: detecting chessboard corners")
                if not self._detect_board_corners(temp_image_path):
                    error_msg = "Failed to detect chessboard corners"
                    self.logger.error(f"❌ {error_msg}")
                    return error_msg, PrimitiveResult.FAILURE
            else:
                self.logger.info("♻️  Using previously detected chessboard corners")
                self.logger.info(f"📍 Stored corners shape: {self.board_corners.shape}")
                self.logger.info(f"📍 Corner coordinates: {self.board_corners}")

            # Step 2: Update board state using vision
            board_updated, detected_move = self._update_board_state(temp_image_path)

            # Step 3: Get best move from Stockfish
            best_move, evaluation = self._get_best_move()
            
            if best_move is None:
                error_msg = "Failed to calculate best move"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Prepare result
            result = {
                "move": best_move,
                "evaluation": evaluation,
                "board_updated": board_updated
            }
            
            self.logger.info("✅ GetChessMove completed successfully")
            self.logger.info(f"📤 Result: {result}")
            
            return result, PrimitiveResult.SUCCESS

        except Exception as e:
            error_msg = f"Unexpected error during chess move calculation: {e}"
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE

        finally:
            # Clean up temporary file
            try:
                if temp_image_path and os.path.exists(temp_image_path):
                    os.unlink(temp_image_path)
                    self.logger.debug(f"Cleaned up temporary file: {temp_image_path}")
            except Exception as e:
                self.logger.warning(f"Failed to clean up temporary file: {e}")

    def cancel(self):
        """Cancel the chess move calculation."""
        self.logger.info("🛑 Canceling chess move calculation")
        
        # If Stockfish is running, we could potentially interrupt it
        # For now, just return a cancellation message
        return "Chess move calculation canceled"

    def __del__(self):
        """Clean up Stockfish engine on destruction."""
        if self.stockfish_engine:
            try:
                self.stockfish_engine.quit()
                self.logger.info("Stockfish engine cleaned up")
            except Exception as e:
                self.logger.warning(f"Error cleaning up Stockfish engine: {e}") 