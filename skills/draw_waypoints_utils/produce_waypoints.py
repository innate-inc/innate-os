"""
python main.py --input_path /Users/rbenefo/Desktop/not-very-intuitive-robot/skills/draw_waypoints/demo/patrick.png --validate True
"""


import re
from typing import List
import argparse
import tempfile

import vtracer
import cv2
from svgpathtools import svg2paths

import datatypes
import validate


FLATTEN_RESOLUTION = 1.0  # smaller = smoother drawing    

def _flatten_path(path, step=1.0):
    """Convert SVG path (curves included) into points."""
    points = []
    for segment in path:
        length = segment.length()
        num_samples = max(int(length / step), 1)

        for i in range(num_samples + 1):
            t = i / num_samples
            point = segment.point(t)
            points.append((point.real, point.imag))
    return points

def _parse_transform_str(transform_str: str) -> datatypes.Waypoint:
    match = re.match(r"translate\((\-?\d+\.?\d*),\s*(\-?\d+\.?\d*)\)", transform_str)
    if not match:
        raise ValueError(f"Invalid format: {transform_str}")
    
    x, y = map(float, match.groups())
    return datatypes.Waypoint(x, y)

def _svg_to_paths(svg_path: str) -> List[List[datatypes.Waypoint]]:
    paths, attributes = svg2paths(svg_path)
    print(f"Extracted {len(paths)} paths from SVG")
    all_paths = []
    if len(paths) == 0:
        return [[]]
    
    ## TODO(rbenefo): First path isn't always the border. Sometimes,
    ## second path is too.
    for path, attr in list(zip(paths, attributes))[1:]:
        ## Skip first path, which is just the border of the image.
        transform_str = attr.get('transform', '')
        if transform_str != '':
            transform = _parse_transform_str(transform_str)
            
        pts = _flatten_path(path, FLATTEN_RESOLUTION)
        if len(pts) > 1:
            all_paths.append([datatypes.Waypoint(x + transform.x, y + transform.y) for x, y in pts])
    return all_paths

def _points_to_commands(paths: List[List[datatypes.Waypoint]]) -> List[datatypes.Action]:
    commands: List[datatypes.Action] = []
    pen_down = False
    for path in paths:
        # Move to start point (pen up)
        if pen_down:
            commands.append(datatypes.Action(datatypes.ActionType.LIFT, None))
            pen_down = False

        start = path[0]
        commands.append(datatypes.Action(datatypes.ActionType.WAYPOINT, datatypes.Waypoint(start.x, start.y)))

        # Drop pen to start drawing
        commands.append(datatypes.Action(datatypes.ActionType.DROP, None))
        pen_down = True

        # Draw rest of path
        for point in path[1:]:
            commands.append(datatypes.Action(datatypes.ActionType.WAYPOINT, datatypes.Waypoint(point.x, point.y)))

    # End with pen up
    if pen_down:
        commands.append(datatypes.Action(datatypes.ActionType.LIFT, None))

    return commands


def produce_waypoints(input_path: str) -> List[datatypes.Action]:
    with tempfile.NamedTemporaryFile(suffix=".svg") as tmp:
        vtracer.convert_image_to_svg_py(input_path, tmp.name)
        paths = _svg_to_paths(tmp.name)
    actions = _points_to_commands(paths)
    return actions

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_path", help="Path to input image")
    parser.add_argument("--validate", help="Validate by visualizing output actions.")
    args = parser.parse_args()

    with tempfile.NamedTemporaryFile(suffix=".svg") as tmp:
        vtracer.convert_image_to_svg_py(args.input_path, tmp.name)
        paths = _svg_to_paths(tmp.name)
    actions = _points_to_commands(paths)

    if args.validate:
        img = cv2.imread(args.input_path, cv2.IMREAD_GRAYSCALE)
        assert img is not None
        actions = _points_to_commands(paths)
        validate.visualize_actions(img, actions)
    

if __name__ == "__main__":
    main()