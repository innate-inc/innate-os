import chess
import numpy as np
import cv2
import sys
import os
from time import time
import logging

# Import the chess detection functions
from .contour_detect import *
from .line_intersection import *
from .rectify_refine import *

def order_points(pts):
    """
    Sorts a list of 4 coordinates into a consistent order:
    top-left, top-right, bottom-right, bottom-left
    """
    rect = np.zeros((4, 2), dtype="float32")
    
    # The top-left point will have the smallest sum (x+y), and the
    # bottom-right point will have the largest sum.
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    
    # The top-right point will have the smallest difference (y-x),
    # and the bottom-left will have the largest difference.
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    
    return rect

def get_board_tiles_with_annotation(image_path, logger, corners=None, save_debug_images=False, debug_prefix=''):
    """
    Processes an image of a chessboard and returns a warped 10x10 image with annotations.
    The chessboard occupies the center 8x8 area with 1 tile padding on each side.
    If corners is provided, uses them directly. Otherwise detects corners.
    Returns None if chessboard detection fails.
    If save_debug_images is True, saves intermediate images for debugging.
    """
    try:
        img = cv2.imread(image_path)
        if img is None:
            logger.error(f"Error: Could not load image from {image_path}")
            return None
            
        img_orig2 = img.copy()

        if corners is None:
            # First time - detect corners using the new function
            from .chess_detection import detectChessboardCorners
            detected_corners, success = detectChessboardCorners(image_path)
            if not success:
                logger.error("Failed to detect chessboard corners")
                return None
            real_corners = detected_corners
        else:
            # Use provided corners
            logger.info("Using provided corners for board analysis")
            real_corners = corners

        # --- Start Robustness Change ---
        # Ensure corners are in a consistent order before warping
        ordered_corners = order_points(real_corners)
        logger.info(f"DEBUG: Corners after re-ordering:\n{ordered_corners}")
        # --- End Robustness Change ---

        # Create transformation that maps detected corners to center of 640x640 image
        # The chessboard should occupy pixels 64-576 in both dimensions (center 512x512)
        # This gives us 64 pixels (1 tile) of padding on each side
        tile_size = 64
        padding = tile_size  # 1 tile worth of padding
        board_size = 8 * tile_size  # 512 pixels for the 8x8 chessboard
        total_size = board_size + 2 * padding  # 640 pixels total
        
        # Destination corners in the final 640x640 image (chessboard occupies center)
        dst_corners = np.array([
            [padding, padding],                    # Top-left: (64, 64)
            [padding + board_size, padding],       # Top-right: (576, 64)  
            [padding + board_size, padding + board_size],  # Bottom-right: (576, 576)
            [padding, padding + board_size]        # Bottom-left: (64, 576)
        ], dtype=np.float32)
        
        logger.info(f"Destination corners in 640x640 image: {dst_corners}")
        
        # --- Start Enhanced Debugging ---
        logger.info(f"DEBUG: Shape of real_corners: {ordered_corners.shape}, dtype: {ordered_corners.dtype}")
        logger.info(f"DEBUG: Value of real_corners:\n{ordered_corners}")
        logger.info(f"DEBUG: Shape of dst_corners: {dst_corners.shape}, dtype: {dst_corners.dtype}")
        logger.info(f"DEBUG: Value of dst_corners:\n{dst_corners}")
        # --- End Enhanced Debugging ---

        # Get perspective transform matrix
        M = cv2.getPerspectiveTransform(ordered_corners.astype(np.float32), dst_corners)
        
        # --- Start Enhanced Debugging ---
        logger.info(f"DEBUG: Perspective transform matrix M:\n{M}")
        # --- End Enhanced Debugging ---

        # Apply transformation to get 640x640 warped image with padding
        warp_img = cv2.warpPerspective(img_orig2, M, (total_size, total_size))
        
        if warp_img is None:
            logger.error("Failed to get final warped image")
            return None
            
        # --- Start Enhanced Debugging ---
        logger.info(f"DEBUG: Shape of warped image: {warp_img.shape}")
        # --- End Enhanced Debugging ---

        # Create annotated version with grid lines and square labels
        annotated_img = warp_img.copy()
        
        # Draw grid lines
        for i in range(11):  # 11 lines for 10 tiles
            x = i * tile_size
            cv2.line(annotated_img, (x, 0), (x, total_size), (0, 255, 255), 2)
            cv2.line(annotated_img, (0, x), (total_size, x), (0, 255, 255), 2)
        
        # Highlight the chessboard area
        cv2.rectangle(annotated_img, (padding, padding), (padding + board_size, padding + board_size), (0, 255, 0), 3)
        
        # Add square labels to the center 8x8 area
        for r in range(8):
            for c in range(8):
                # Calculate position in the 10x10 grid (add 1 for padding)
                grid_r = r + 1
                grid_c = c + 1
                
                # Calculate center of the square
                center_x = grid_c * tile_size + tile_size // 2
                center_y = grid_r * tile_size + tile_size // 2
                
                # Chess notation (a1 is bottom-left, h8 is top-right)
                chess_file = chr(ord('a') + c)
                chess_rank = str(8 - r)
                square_name = chess_file + chess_rank
                
                # Add text label
                cv2.putText(annotated_img, square_name, (center_x - 15, center_y + 5), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                cv2.putText(annotated_img, square_name, (center_x - 15, center_y + 5), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)

        # Save debug images if requested
        if save_debug_images:
            debug_dir = "/tmp/chess_debug"
            os.makedirs(debug_dir, exist_ok=True)
            timestamp = int(time())
            prefix = f"{debug_prefix}_" if debug_prefix else ""
            
            # Save the original warped image
            debug_warped_path = os.path.join(debug_dir, f"{prefix}warped_board_padded_{timestamp}.png")
            cv2.imwrite(debug_warped_path, warp_img, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            logger.info(f"🔍 Debug: Saved padded warped image to {debug_warped_path}")
            
            # Save the annotated image
            debug_annotated_path = os.path.join(debug_dir, f"{prefix}annotated_board_{timestamp}.png")
            cv2.imwrite(debug_annotated_path, annotated_img, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            logger.info(f"🔍 Debug: Saved annotated board to {debug_annotated_path}")
            
            # Also save the original image with detected corners for reference
            debug_corners_img = img_orig2.copy()
            for i, corner in enumerate(ordered_corners):
                cv2.circle(debug_corners_img, tuple(corner.astype(int)), 10, (0, 255, 0), -1)
                cv2.putText(debug_corners_img, str(i), tuple(corner.astype(int)), 
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            debug_corners_path = os.path.join(debug_dir, f"{prefix}corners_detected_{timestamp}.png")
            cv2.imwrite(debug_corners_path, debug_corners_img, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            logger.info(f"🔍 Debug: Saved corners image to {debug_corners_path}")
            
        return annotated_img
        
    except Exception as e:
        logger.error(f"Error processing image: {e}")
        return None

def create_annotated_board_images(before_image_path, after_image_path, logger, corners=None, save_debug_images=False):
    """
    Create annotated board images for both before and after move states.
    
    Args:
        before_image_path: Path to image before the move
        after_image_path: Path to image after the move
        corners: Chessboard corners (if None, will detect from before image)
        save_debug_images: Whether to save debug images
        
    Returns:
        tuple: (before_annotated_image, after_annotated_image, corners_used)
    """
    # First, get corners from the before image if not provided
    if corners is None:
        try:
            from .chess_detection import detectChessboardCorners
            corners, success = detectChessboardCorners(before_image_path)
            if not success:
                logger.error("Failed to detect chessboard corners from before image")
                return None, None, None
        except Exception as e:
            logger.error(f"Error detecting corners: {e}")
            return None, None, None
    
    # Create annotated images for both states
    before_annotated = get_board_tiles_with_annotation(before_image_path, logger, corners=corners, save_debug_images=save_debug_images, debug_prefix='before')
    after_annotated = get_board_tiles_with_annotation(after_image_path, logger, corners=corners, save_debug_images=save_debug_images, debug_prefix='after')
    
    if before_annotated is None or after_annotated is None:
        logger.error("Failed to create annotated images")
        return None, None, None
    
    return before_annotated, after_annotated, corners

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python update_board_state.py <before_image_path> <after_image_path> [corners_file]")
        print("Example: python update_board_state.py before.jpg after.jpg")
        sys.exit(1)
    
    before_image_path = sys.argv[1]
    after_image_path = sys.argv[2]
    
    # Create a basic logger for command-line use
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.info(f"Processing before image: {before_image_path}")
    logger.info(f"Processing after image: {after_image_path}")
    logger.info("-" * 60)
    
    try:
        before_annotated, after_annotated, corners = create_annotated_board_images(
            before_image_path, after_image_path, logger, save_debug_images=True
        )
        
        if before_annotated is not None and after_annotated is not None:
            logger.info("✅ Successfully created annotated board images")
            logger.info("🔍 Check /tmp/chess_debug/ for debug images")
        else:
            logger.error("❌ Failed to create annotated board images")
            
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1) 