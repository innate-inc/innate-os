import chess
import numpy as np
import cv2
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import sys
import os
from time import time

# Import the chess detection functions
from .contour_detect import *
from .line_intersection import *
from .rectify_refine import *

# CNN model for chess piece classification (matches the trained model)
class ChessCNN(nn.Module):
    def __init__(self, num_classes=3):
        super().__init__()
        IMG_SIZE = (64, 64)  # Based on the 64x64 tiles used in this code
        self.features = nn.Sequential(
            # Block 1
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.25),
            # Block 2
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.25),
            # Block 3
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Dropout(0.25),
        )
        flat_dim = 128 * (IMG_SIZE[0]//8) * (IMG_SIZE[1]//8)  # 8×8 feature map
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(flat_dim, 128),
            nn.ReLU(),
            nn.BatchNorm1d(128),
            nn.Dropout(0.5),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        x = self.features(x)
        return self.classifier(x)

# Device for PyTorch operations
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Transform for preprocessing images
transform = transforms.Compose([
    transforms.ToPILImage(),
    transforms.Resize((64, 64)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

def get_board_tiles(image_path, corners=None, save_debug_images=False):
    """
    Processes an image of a chessboard and returns a list of 64 tiles (64x64 each).
    If corners is provided, uses them directly. Otherwise detects corners.
    Returns None if chessboard detection fails.
    If save_debug_images is True, saves intermediate images for debugging.
    """
    try:
        img = cv2.imread(image_path)
        if img is None:
            print(f"Error: Could not load image from {image_path}")
            return None
            
        img = scaleImageIfNeeded(img, 1024, 768)
        img_orig2 = img.copy()

        if corners is None:
            # First time - detect corners using the new function
            from chess_detection import detectChessboardCorners
            detected_corners, success = detectChessboardCorners(image_path)
            if not success:
                print("Failed to detect chessboard corners")
                return None
            real_corners = detected_corners
        else:
            # Use provided corners
            print("Using provided corners for board analysis")
            real_corners = corners

        # Get final refined rectified warped image - crop exactly to chessboard
        warp_img, _ = getTileImageExact(img_orig2, real_corners, tile_res=64)
        
        if warp_img is None:
            print("Failed to get final warped image")
            return None

        # Save debug images if requested
        if save_debug_images:
            debug_dir = "/tmp/chess_debug"
            os.makedirs(debug_dir, exist_ok=True)
            
            # Save the warped image at full resolution
            debug_warped_path = os.path.join(debug_dir, f"warped_board_{int(time())}.jpg")
            cv2.imwrite(debug_warped_path, warp_img)
            print(f"🔍 Debug: Saved warped image to {debug_warped_path}")
            
            # Also save the original image with detected corners for reference
            debug_corners_img = img_orig2.copy()
            for i, corner in enumerate(real_corners):
                cv2.circle(debug_corners_img, tuple(corner.astype(int)), 10, (0, 255, 0), -1)
                cv2.putText(debug_corners_img, str(i), tuple(corner.astype(int)), 
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            debug_corners_path = os.path.join(debug_dir, f"corners_detected_{int(time())}.jpg")
            cv2.imwrite(debug_corners_path, debug_corners_img)
            print(f"🔍 Debug: Saved corners image to {debug_corners_path}")

        # Resize to 512x512 and extract 64x64 tiles
        resized_img = cv2.resize(warp_img, (512, 512))
        
        tiles = []
        tile_size = 64
        for r in range(8):
            for c in range(8):
                y_start = r * tile_size
                y_end = y_start + tile_size
                x_start = c * tile_size
                x_end = x_start + tile_size
                tile = resized_img[y_start:y_end, x_start:x_end]
                tiles.append(tile)
                
                # Save all tiles for debugging
                if save_debug_images:
                    debug_dir = "/tmp/chess_debug"
                    square_name = chr(ord('a') + c) + str(8 - r)  # Convert to chess notation
                    tile_path = os.path.join(debug_dir, f"tile_{square_name}_{int(time())}.jpg")
                    cv2.imwrite(tile_path, tile)
                    print(f"🔍 Debug: Saved tile {square_name} to {tile_path}")
        
        return tiles
        
    except Exception as e:
        print(f"Error processing image: {e}")
        return None

def classify_tile(tile, model):
    """
    Classifies a single 64x64 tile image using the PyTorch CNN model.
    Returns a tuple of (class_name, confidence_scores_dict)
    """
    if model is None:
        print("Error: Model not loaded. Cannot classify tile.")
        return "no_piece", {"black_piece": 0.0, "no_piece": 1.0, "white_piece": 0.0}
    
    try:
        # Convert BGR to RGB
        tile_rgb = cv2.cvtColor(tile, cv2.COLOR_BGR2RGB)
        
        # Apply transforms
        input_tensor = transform(tile_rgb).unsqueeze(0).to(device)
        
        # Run prediction
        with torch.no_grad():
            outputs = model(input_tensor)
            probabilities = F.softmax(outputs, dim=1)[0]
            
        class_names = ["black_piece", "no_piece", "white_piece"]
        idx = torch.argmax(probabilities).item()
        
        # Create confidence scores dictionary
        confidence_scores = {class_names[i]: float(probabilities[i]) for i in range(len(class_names))}
        
        return class_names[idx], confidence_scores
        
    except Exception as e:
        print(f"Error classifying tile: {e}")
        return "no_piece", {"black_piece": 0.0, "no_piece": 1.0, "white_piece": 0.0}

def get_new_fen(image_path, old_fen, model, confidence_threshold=0.99, corners=None, save_debug_images=False):
    """
    Takes an image of a chessboard and the previous board state in FEN,
    and returns the new board state in FEN and the move that was made.
    Only considers vision detections with confidence >= confidence_threshold.
    If corners is provided, uses them directly. Otherwise detects corners.
    If save_debug_images is True, saves debug images for inspection.
    """
    # 1. Get the 64 tiles from the image
    tiles = get_board_tiles(image_path, corners=corners, save_debug_images=save_debug_images)
    if not tiles or len(tiles) != 64:
        print("Could not detect a chessboard or extract 64 tiles.")
        return old_fen, None

    # 2. Classify each tile
    tile_classifications = [classify_tile(tile, model) for tile in tiles]
    vision_board_colors = [classification[0] for classification in tile_classifications]
    vision_board_confidences = [classification[1] for classification in tile_classifications]
    
    # Debug: Print first few classifications
    print("First 10 tile classifications:")
    for i in range(min(10, len(vision_board_colors))):
        confidence = vision_board_confidences[i][vision_board_colors[i]]
        print(f"  Tile {i}: {vision_board_colors[i]} (confidence: {confidence:.3f})")
    
    # Count classifications
    class_counts = {}
    for cls in vision_board_colors:
        class_counts[cls] = class_counts.get(cls, 0) + 1
    print(f"Classification counts: {class_counts}")
    
    # Debug: Print the board layout as seen by vision
    print("\nBoard layout (as detected by vision):")
    print("   a  b  c  d  e  f  g  h")
    for r in range(8):
        row_display = f"{8-r}: "
        for c in range(8):
            square_index = r * 8 + c
            vision_class = vision_board_colors[square_index]
            if vision_class == "white_piece":
                symbol = "W"
            elif vision_class == "black_piece":
                symbol = "B"
            else:
                symbol = "."
            row_display += f"{symbol}  "
        print(row_display)
    
    # Print expected layout from FEN
    print(f"\nExpected board layout from FEN: {old_fen}")
    board_display = chess.Board(old_fen)
    print(board_display)

    # 3. Use chess logic to determine the new FEN
    board = chess.Board(old_fen)
    
    # Find differences between old board state and vision
    changed_squares = []
    ignored_low_confidence = 0
    
    for r in range(8):
        for c in range(8):
            square_index = r * 8 + c
            # Handle 180-degree rotation: flip both row and column
            square = chess.square(7 - c, r)  # Convert to chess square notation (180° rotated)
            
            piece = board.piece_at(square)
            vision_color_str = vision_board_colors[square_index]
            vision_confidence = vision_board_confidences[square_index][vision_color_str]
            
            # Only consider high-confidence detections
            if vision_confidence < confidence_threshold:
                ignored_low_confidence += 1
                continue  # Skip low-confidence detections
            
            # Convert vision result to piece color
            vision_color = None
            if vision_color_str == "white_piece":
                vision_color = chess.WHITE
            elif vision_color_str == "black_piece":
                vision_color = chess.BLACK
            # vision_color is None for "no_piece"

            # Get current piece color (None if no piece)
            piece_color = piece.color if piece else None

            # Check if there's a difference
            if piece_color != vision_color:
                changed_squares.append({
                    'square': square,
                    'old_color': piece_color,
                    'new_color': vision_color,
                    'old_piece': piece,
                    'square_name': chess.square_name(square)
                })
    
    print(f"Ignored {ignored_low_confidence} squares due to low confidence (< {confidence_threshold})")
    print(f"Detected {len(changed_squares)} changed squares:")
    for change in changed_squares:
        old_desc = f"{change['old_piece']}" if change['old_piece'] else "empty"
        
        # Get the square index for confidence lookup
        # Reverse the transformation: square = chess.square(7 - c, r)
        # So: chess_file = 7 - c, chess_rank = r
        # Therefore: c = 7 - chess_file, r = chess_rank
        r = chess.square_rank(change['square'])
        c = 7 - chess.square_file(change['square'])
        square_index = r * 8 + c
        
        if change['new_color'] == chess.WHITE:
            new_desc = "white piece"
        elif change['new_color'] == chess.BLACK:
            new_desc = "black piece"
        else:
            new_desc = "empty"
        
        # Get confidence scores for this square
        confidence_scores = vision_board_confidences[square_index]
        vision_class = vision_board_colors[square_index]
        main_confidence = confidence_scores[vision_class]
        
        # Show all confidence scores for changed squares
        conf_str = ", ".join([f"{cls}: {conf:.3f}" for cls, conf in confidence_scores.items()])
        
        print(f"  {change['square_name']}: {old_desc} -> {new_desc}")
        print(f"    Vision confidence: {vision_class} ({main_confidence:.3f}) | All: [{conf_str}]")
    
    # Try to find a valid move that explains the changes
    if len(changed_squares) == 0:
        print("No changes detected.")
        return old_fen, None
    
    # Generate all legal moves and see which one matches the observed changes
    legal_moves = list(board.legal_moves)
    
    for move in legal_moves:
        # Create a test board to see what this move would produce
        test_board = board.copy()
        test_board.push(move)
        
        # Check if this move explains all the observed changes
        move_explains_changes = True
        
        for change in changed_squares:
            square = change['square']
            expected_piece = test_board.piece_at(square)
            expected_color = expected_piece.color if expected_piece else None
            observed_color = change['new_color']
            
            if expected_color != observed_color:
                move_explains_changes = False
                break
        
        if move_explains_changes:
            print(f"Found matching move: {move}")
            return test_board.fen(), move
    
    # If no single legal move explains the changes, try to handle special cases
    
    # Case 1: Simple piece movement (one piece disappears, one appears)
    disappeared_squares = [c for c in changed_squares if c['old_color'] is not None and c['new_color'] is None]
    appeared_squares = [c for c in changed_squares if c['old_color'] is None and c['new_color'] is not None]
    
    if len(disappeared_squares) == 1 and len(appeared_squares) == 1:
        from_sq = disappeared_squares[0]['square']
        to_sq = appeared_squares[0]['square']
        moving_piece = disappeared_squares[0]['old_piece']
        
        # Verify the colors match
        if moving_piece and moving_piece.color == appeared_squares[0]['new_color']:
            # Try to construct the move
            try:
                # Handle promotion case
                move = chess.Move(from_sq, to_sq)
                if move in legal_moves:
                    new_board = board.copy()
                    new_board.push(move)
                    print(f"Applied simple move: {move}")
                    return new_board.fen(), move
                else:
                    # Try promotion moves
                    for promotion_piece in [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]:
                        promo_move = chess.Move(from_sq, to_sq, promotion=promotion_piece)
                        if promo_move in legal_moves:
                            new_board = board.copy()
                            new_board.push(promo_move)
                            print(f"Applied promotion move: {promo_move}")
                            return new_board.fen(), promo_move
            except:
                pass
    
    # Case 2: Capture (one piece disappears, one changes color)
    if len(changed_squares) == 2:
        # Try to identify capture scenarios
        for i, change1 in enumerate(changed_squares):
            for j, change2 in enumerate(changed_squares):
                if i != j:
                    # Check if change1 is the moving piece and change2 is the capture
                    if (change1['old_color'] is not None and change1['new_color'] is None and
                        change2['old_color'] is not None and change2['new_color'] is not None and
                        change1['old_color'] == change2['new_color']):
                        
                        from_sq = change1['square']
                        to_sq = change2['square']
                        move = chess.Move(from_sq, to_sq)
                        
                        if move in legal_moves:
                            new_board = board.copy()
                            new_board.push(move)
                            print(f"Applied capture move: {move}")
                            return new_board.fen(), move
    
    print(f"Could not determine move from {len(changed_squares)} changes. Returning old FEN.")
    return old_fen, None

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) != 3:
        print("Usage: python update_board_state.py <image_path> <old_fen>")
        print("Example: python update_board_state.py chess_image.jpg 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'")
        sys.exit(1)
    
    image_path = sys.argv[1]
    old_fen = sys.argv[2]
    
    print(f"Processing image: {image_path}")
    print(f"Old FEN: {old_fen}")
    print(f"Using confidence threshold: 0.99")
    print("-" * 60)
    
    try:
        # For standalone usage, try to load model
        model = None
        MODEL_PATH = "chess_square_classifier.pth"
        if os.path.exists(MODEL_PATH):
            try:
                model = ChessCNN()
                model.load_state_dict(torch.load(MODEL_PATH, map_location=device))
                model.to(device)
                model.eval()
                print(f"Loaded chess piece classifier model on {device}.")
            except Exception as e:
                print(f"Error loading model: {e}")
        else:
            print(f"Warning: Model not found at {MODEL_PATH}. Tile classification will fail.")
        
        new_fen, move = get_new_fen(image_path, old_fen, model, confidence_threshold=0.99, save_debug_images=True)
        print("-" * 60)
        print(f"New FEN: {new_fen}")
        
        # Show the difference if there was a change
        if new_fen != old_fen:
            print("\nBoard state updated successfully!")
            old_board = chess.Board(old_fen)
            new_board = chess.Board(new_fen)
            print(f"Turn: {old_board.turn} -> {new_board.turn}")

            if move:
                move_san = old_board.san(move)
                
                from_sq_name = chess.square_name(move.from_square)
                to_sq_name = chess.square_name(move.to_square)
                
                moving_piece = old_board.piece_at(move.from_square)
                
                description = f""
                
                if moving_piece:
                    moving_piece_color = "White" if moving_piece.color == chess.WHITE else "Black"
                    moving_piece_name = chess.piece_name(moving_piece.piece_type).lower()
                    description += f"{moving_piece_color} {moving_piece_name} from {from_sq_name} to {to_sq_name}"
                
                if old_board.is_capture(move):
                    captured_piece = old_board.piece_at(move.to_square)
                    if captured_piece:
                        captured_piece_color = "White" if captured_piece.color == chess.WHITE else "Black"
                        captured_piece_name = chess.piece_name(captured_piece.piece_type).lower()
                        description += f", capturing {captured_piece_color} {captured_piece_name}"
                    elif old_board.is_en_passant(move):
                        description += ", capturing by en passant"

                if old_board.is_castling(move):
                    description = f"Move description ({move_san}): {moving_piece_color} castling"
                
                if move.promotion:
                    promo_piece_name = chess.piece_name(move.promotion).lower()
                    description += f", promoting to a {promo_piece_name}"

                print(description + ".")

        else:
            print("\nNo changes detected or move could not be determined.")
            
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1) 