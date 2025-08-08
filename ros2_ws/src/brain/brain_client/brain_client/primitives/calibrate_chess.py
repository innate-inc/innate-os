#!/usr/bin/env python3
import cv2
import numpy as np
import tempfile
import os
import time
import json
import shutil
import traceback
import pkg_resources
from brain_client.primitives.types import Primitive, PrimitiveResult, RobotStateType
from brain_client.utils.camera_utils import initialize_camera
from brain_client.utils.chess.chess_detection import detectChessboardCorners


class CalibrateChess(Primitive):
    """
    Primitive for calibrating the chess vision system by detecting chessboard corners.
    This should be run once before starting a chess game to establish the board perspective.
    It now also resets the initial board state and computes interpolated Cartesian poses.
    """

    def __init__(self, logger):
        super().__init__(logger)
        
        # Camera configuration
        self.camera_index = None  # Will be determined automatically
        self.camera = None
        self.preferred_backend = cv2.CAP_V4L2  # Use V4L2 backend for better compatibility
        
        # Path for the initial board setup image, used by get_chess_move
        self.initial_board_image_path = "/tmp/initial_board_setup.jpg"
        
        # Path to the FEN file that holds the game's state
        self.fen_file_path = "/tmp/chess_game_fen.txt"
        self.move_history_path = "/tmp/chess_move_history.txt"
        self.fen_history_path = "/tmp/chess_fen_history.txt"

        # Path for the generated cartesian poses
        self.cartesian_poses_path = "/tmp/chess_cartesian_poses.json"
        
        # Path for the chessboard corner data
        self.corners_file_path = "/tmp/chess_board_corners.npy"
        
        self.logger.info("CalibrateChess primitive initialized")

    @property
    def name(self):
        return "calibrate_chess"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """This primitive doesn't need robot state since it only captures images."""
        return []

    def update_robot_state(self, **kwargs):
        """No robot state needed for calibration."""
        pass

    def _interpolate_and_save_poses(self):
        """
        Load anchor poses, interpolate for all squares, and save to a file.
        """
        try:
            # 1. Load anchor poses from the package file
            anchor_path = pkg_resources.resource_filename('brain_client', 'utils/chess/chess_anchors.json')
            with open(anchor_path, 'r') as f:
                anchor_data = json.load(f)

            self.logger.info(f"Loaded anchor poses from {anchor_path}")

            measured_poses = {
                'a1': np.array(anchor_data['anchors']['a1_low']['position']),
                'h1': np.array(anchor_data['anchors']['h1_low']['position']),
                'a4': np.array(anchor_data['anchors']['a4_low']['position']),
                'h4': np.array(anchor_data['anchors']['h4_low']['position'])
            }
            z_offset = anchor_data.get('z_offset', 0.05)
            default_rpy = anchor_data.get('default_orientation_rpy', [0, 1.57, 0.0])

            # 2. Interpolate to find all low poses
            a1 = measured_poses['a1']
            h1 = measured_poses['h1']
            a4 = measured_poses['a4']
            
            file_step = (h1 - a1) / 7.0
            rank_step = (a4 - a1) / 3.0
            
            files = ['a','b','c','d','e','f','g','h']
            ranks = list(range(1,5))
            
            all_poses = {}
            for i, f in enumerate(files):
                for j, r in enumerate(ranks):
                    square = f + str(r)
                    # Low pose (interpolated)
                    low_pos = a1 + i * file_step + j * rank_step
                    # High pose (add z-offset)
                    high_pos = low_pos + np.array([0.0, 0.0, z_offset])
                    
                    all_poses[f"{square}_low"] = {
                        "position": low_pos.tolist(),
                        "orientation_rpy": default_rpy
                    }
                    all_poses[f"{square}_high"] = {
                        "position": high_pos.tolist(),
                        "orientation_rpy": default_rpy
                    }
            
            # 3. Save to the output file
            output_data = {
                "metadata": {
                    "description": "Interpolated Cartesian poses for all chess squares.",
                    "source": "calibrate_chess primitive",
                    "timestamp": time.time()
                },
                "poses": all_poses
            }
            
            with open(self.cartesian_poses_path, 'w') as f:
                json.dump(output_data, f, indent=2)

            self.logger.info(f"✅ Successfully interpolated and saved {len(all_poses)} Cartesian poses to {self.cartesian_poses_path}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Failed to interpolate and save poses: {e}")
            traceback.print_exc()
            return False

    def _initialize_camera(self):
        """Initialize the camera for capturing images using the utility function."""
        if self.camera is not None and self.camera.isOpened():
            return True  # Already initialized
            
        self.camera, self.camera_index = initialize_camera(self.logger, self.camera_index, self.preferred_backend)
        return self.camera is not None

    def _capture_and_save_initial_image(self):
        """Capture an image and save it as the initial board setup."""
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
                self.logger.error("❌ Failed to capture calibration frame from camera")
                return None
            
            # Save the captured frame to the fixed initial board path
            success = cv2.imwrite(self.initial_board_image_path, frame)
            
            if not success:
                self.logger.error(f"❌ Failed to save initial board image to {self.initial_board_image_path}")
                return None
            
            self.logger.info(f"📸 Captured and saved initial board image to: {self.initial_board_image_path}")
            return self.initial_board_image_path
            
        except Exception as e:
            self.logger.error(f"❌ Error capturing initial board image: {e}")
            # Make sure camera is released even on error
            if self.camera is not None:
                try:
                    self.camera.release()
                    cv2.destroyAllWindows()
                    self.camera = None
                except:
                    pass
            return None

    def _detect_and_save_corners(self, image_path):
        """Detect chessboard corners and save them for future use."""
        try:
            self.logger.info("🔍 Detecting chessboard corners for calibration")
            
            # Save a debug copy of the image for inspection
            debug_image_path = f"/tmp/chess_calibration_view_{int(time.time())}.jpg"
            shutil.copy2(image_path, debug_image_path)
            self.logger.info(f"🔍 Debug: Saved calibration view to {debug_image_path}")
            
            # Detect the corners
            corners, success = detectChessboardCorners(image_path)
            
            if success and corners is not None:
                self.logger.info("✅ Successfully detected chessboard corners")
                self.logger.info(f"📍 Corner coordinates: {corners}")
                
                # Save corners to file for other primitives to use
                np.save(self.corners_file_path, corners)
                
                self.logger.info(f"💾 Saved corners to: {self.corners_file_path}")
                
                # Create a visualization showing the detected corners
                img = cv2.imread(image_path)
                if img is not None:
                    # Draw the corners
                    for i, corner in enumerate(corners):
                        cv2.circle(img, tuple(corner.astype(int)), 10, (0, 255, 0), -1)
                        cv2.putText(img, str(i), tuple(corner.astype(int)), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                    
                    # Save visualization
                    viz_path = f"/tmp/chess_corners_visualization_{int(time.time())}.jpg"
                    cv2.imwrite(viz_path, img)
                    self.logger.info(f"🎨 Saved corner visualization to: {viz_path}")
                
                return True
            else:
                self.logger.error("❌ Failed to detect chessboard corners")
                self.logger.error(f"💡 Please check the debug image at: {debug_image_path}")
                self.logger.error("💡 Make sure the chessboard is visible, well-lit, and properly positioned")
                self.logger.error("💡 Ensure all 4 corners of the chessboard are visible in the image")
                return False
                
        except Exception as e:
            self.logger.error(f"Error during corner detection: {e}")
            traceback.print_exc()
            return False

    def guidelines(self):
        return (
            "Use this to calibrate the chess vision system. This primitive will capture "
            "an image from the webcam and detect the chessboard corners. The detected "
            "corners will be saved and used by other chess primitives (play_move and "
            "get_chess_move) for consistent board perspective. Run this once before "
            "starting a chess game. Make sure the chessboard is well-lit and all 4 "
            "corners are visible in the camera view."
        )

    def execute(self, **kwargs):
        """
        Execute the chess calibration process.
        
        Returns:
            tuple: (result_message, result_status)
        """
        self.logger.info("🚀 Starting chess calibration")
        
        # Step 1: Interpolate and save the Cartesian poses for the robot arm
        self._send_feedback("📐 Interpolating Cartesian poses from anchors")
        if not self._interpolate_and_save_poses():
            error_msg = "Failed to interpolate and save Cartesian poses"
            self.logger.error(f"❌ {error_msg}")
            return error_msg, PrimitiveResult.FAILURE

        calibration_image_path = None
        
        try:
            # Step 2: Capture and save the initial board image for vision
            self._send_feedback("📸 Capturing new initial board image")
            calibration_image_path = self._capture_and_save_initial_image()
            if not calibration_image_path:
                error_msg = "Failed to capture and save the initial board image"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 3: Detect and save corners from the new image
            self._send_feedback("🔍 Detecting chessboard corners")
            if not self._detect_and_save_corners(calibration_image_path):
                error_msg = "Failed to detect chessboard corners"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Step 4: CRITICAL - Set this image as the definitive initial board state
            definitive_state_path = "/tmp/last_known_board_state.jpg"
            shutil.copy2(calibration_image_path, definitive_state_path)
            self.logger.info(f"✅ Set initial board state at: {definitive_state_path}")

            # Step 5: Verify interpolated poses file was saved
            if not os.path.exists(self.cartesian_poses_path):
                error_msg = "Interpolated poses file was not created"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE
            
            # Step 6: Create/reset the FEN file to the starting position
            try:
                initial_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
                with open(self.fen_file_path, 'w') as f:
                    f.write(initial_fen)
                self.logger.info(f"✅ Reset chess game state. FEN file created at {self.fen_file_path}")

                # Reset history files
                with open(self.move_history_path, 'w') as f:
                    f.write("")
                with open(self.fen_history_path, 'w') as f:
                    # Start FEN history with the initial state
                    f.write(initial_fen + '\n')
                self.logger.info("✅ Cleared move and FEN history files.")

            except Exception as e:
                error_msg = f"Failed to create FEN or history files: {e}"
                self.logger.error(f"❌ {error_msg}")
                return error_msg, PrimitiveResult.FAILURE

            # Success!
            result_msg = f"✅ Chess recalibration complete! Interpolated poses, new baseline image, corners, and FEN state saved."
            self._send_feedback(result_msg)
            self.logger.info(result_msg)
            
            # Return calibration info
            result = {
                "calibrated": True,
                "cartesian_poses_file": self.cartesian_poses_path,
                "corners_file": self.corners_file_path,
                "initial_image": self.initial_board_image_path,
                "timestamp": time.time()
            }
            
            result_message = json.dumps(result)
            return result_message, PrimitiveResult.SUCCESS

        except Exception as e:
            error_msg = f"Unexpected error during chess calibration: {e}"
            self.logger.error(f"❌ {error_msg}")
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
            
            # The initial board image should not be deleted
            # Clean up temporary calibration image - no longer needed as we use a fixed path
            pass

    def cancel(self):
        """Cancel the calibration operation."""
        self.logger.info("🛑 Canceling chess calibration")
        
        # Release camera if it's open
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()
                self.camera = None
                self.logger.info("📹 Camera released")
            except Exception as e:
                self.logger.warning(f"Error releasing camera: {e}")
        
        return "Chess calibration canceled"

    def __del__(self):
        """Clean up camera on destruction."""
        if self.camera is not None:
            try:
                self.camera.release()
                cv2.destroyAllWindows()
                self.logger.info("Camera released in destructor")
            except Exception as e:
                self.logger.warning(f"Error releasing camera in destructor: {e}")

    @staticmethod
    def load_calibrated_corners(logger=None):
        """
        Static method to load calibrated corners from file.
        This can be used by other primitives to get the saved corners.
        
        Returns:
            numpy.ndarray or None: The calibrated corners or None if not found
        """
        corners_file_path = "/tmp/chess_board_corners.npy"  # Use numpy's native format
        
        try:
            if not os.path.exists(corners_file_path):
                if logger:
                    logger.warning(f"Calibration file not found at {corners_file_path}")
                else:
                    print(f"Warning: Calibration file not found at {corners_file_path}")
                return None
            
            # Load numpy array directly
            corners = np.load(corners_file_path)
            if logger:
                logger.info(f"✅ Loaded corners from {corners_file_path}")
            
            return corners
            
        except Exception as e:
            if logger:
                logger.error(f"Error loading calibrated corners: {e}")
            else:
                print(f"Error loading calibrated corners: {e}")
            return None

    @staticmethod
    def is_calibrated():
        """
        Static method to check if the chess system has been calibrated.
        Now checks for both the interpolated poses file and the corner file.
        
        Returns:
            bool: True if calibrated, False otherwise
        """
        poses_file_path = "/tmp/chess_cartesian_poses.json"
        corners_file_path = "/tmp/chess_board_corners.npy"
        return os.path.exists(poses_file_path) and os.path.exists(corners_file_path) 