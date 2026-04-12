"""
python demo/main.py --input_path "/Users/rbenefo/Desktop/not-very-intuitive-robot/innate-os/skills/draw_waypoints_utils/demo/single_line.jpg" --validate True
"""

import argparse
import os
import sys

import cv2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from produce_waypoints import produce_waypoints
import validate

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", help="Path to input image")
    parser.add_argument("--validate", help="Validate by visualizing output actions.")
    args = parser.parse_args()
    
    assert os.path.exists(args.input_path), f"Input path {args.input_path} does not exist"
    actions = produce_waypoints(args.input_path)
    if args.validate:
        img = cv2.imread(args.input_path, cv2.IMREAD_GRAYSCALE)
        assert img is not None
        validate.visualize_actions(img, actions)    

if __name__ == "__main__":
    main()