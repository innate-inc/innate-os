from stl import mesh
from PIL import Image, ImageDraw
import numpy as np


def slice_stl(stl_path, height, output_path, pixel_size=0.05):
    """
    Create a 2D PNG slice of an STL file at a specified height.
    Returns a tuple (occupancy_grid, bounds) where bounds is a dict containing:
      - min_x, min_y, max_x, max_y of the mesh,
      - scale, offset_x, offset_y used in the image transform.

    Note: This version removes the extra buffer (the 0.8 factor previously applied)
    so that (0,0) in the occupancy grid corresponds exactly to (min_x, min_y) of the mesh.
    """
    # Load the STL file
    model = mesh.Mesh.from_file(stl_path)

    # Get mesh bounds
    min_z = model.z.min()
    max_z = model.z.max()
    min_x = model.x.min()
    max_x = model.x.max()
    min_y = model.y.min()
    max_y = model.y.max()

    if height < min_z or height > max_z:
        raise ValueError(
            f"Height {height}m is outside model bounds ({min_z}m to {max_z}m)"
        )

    # Compute the world dimensions in meters
    world_width = max_x - min_x
    world_height = max_y - min_y

    # Get image size based on desired pixel size (each pixel represents pixel_size meters)
    image_size = (int(world_width / pixel_size), int(world_height / pixel_size))

    # Create a new image with a black background
    img = Image.new("RGB", image_size, "black")
    draw = ImageDraw.Draw(img)

    # Remove buffer: use a direct mapping so that
    # scale = 1 / pixel_size and offset ensures that (min_x, min_y) maps to (0, 0)
    scale = 1.0 / pixel_size
    offset_x = -min_x * scale
    offset_y = -min_y * scale

    # Process intersections for each triangle in the mesh
    for triangle in model.vectors:
        points = []
        for i in range(3):
            p1 = triangle[i]
            p2 = triangle[(i + 1) % 3]

            # Check if the edge crosses the slice plane
            if (p1[2] <= height <= p2[2]) or (p2[2] <= height <= p1[2]):
                if p2[2] - p1[2] != 0:  # Avoid division by zero
                    # Compute the intersection parameter t
                    t = (height - p1[2]) / (p2[2] - p1[2])
                    x = p1[0] + t * (p2[0] - p1[0])
                    y = p1[1] + t * (p2[1] - p1[1])

                    # Transform the world coordinates to image coordinates
                    img_x = int(x * scale + offset_x)
                    img_y = int(y * scale + offset_y)
                    points.append((img_x, img_y))
        if len(points) == 2:
            draw.line(points, fill="white", width=2)

    # Convert the PIL image to a numpy array
    occupancy_grid = np.array(img, dtype=np.uint8)
    bounds = {
        "min_x": min_x,
        "min_y": min_y,
        "max_x": max_x,
        "max_y": max_y,
        "scale": scale,
        "offset_x": offset_x,
        "offset_y": offset_y,
        "pixel_size": pixel_size,
    }
    return occupancy_grid, bounds
