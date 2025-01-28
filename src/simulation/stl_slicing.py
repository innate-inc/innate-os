from stl import mesh
from PIL import Image, ImageDraw
import numpy as np


def slice_stl(stl_path, height, output_path, pixel_size=0.05):
    """
    Create a 2D PNG slice of an STL file at a specified height.

    Args:
        stl_path (str): Path to the input STL file
        height (float): Height at which to slice, in meters
        output_path (str): Path for the output PNG file
        pixel_size (float): Size of each pixel in meters

    Returns:
        numpy.ndarray: The slice image as a numpy array
    """
    # Load the STL file
    model = mesh.Mesh.from_file(stl_path)

    # Get model dimensions
    min_z = model.z.min()
    max_z = model.z.max()
    min_x = model.x.min()
    max_x = model.x.max()
    min_y = model.y.min()
    max_y = model.y.max()

    # Remove percentage calculation and use height directly
    if height < min_z or height > max_z:
        raise ValueError(
            f"Height {height}m is outside model bounds ({min_z}m to {max_z}m)"
        )

    # Define image_size on both axes so that each pixel is 5cm, knowing that max_x and co are in meters
    image_size = (int((max_x - min_x) / pixel_size), int((max_y - min_y) / pixel_size))

    # Create a new image with white background
    img = Image.new("RGB", image_size, "white")
    draw = ImageDraw.Draw(img)

    # Scale factors to fit the model in the image
    scale_x = (image_size[0] * 0.8) / (max_x - min_x)
    scale_y = (image_size[1] * 0.8) / (max_y - min_y)
    scale = min(scale_x, scale_y)

    # Offset to center the model
    offset_x = image_size[0] / 2 - ((max_x + min_x) / 2) * scale
    offset_y = image_size[1] / 2 - ((max_y + min_y) / 2) * scale

    # Process each triangle in the mesh
    for triangle in model.vectors:
        # Find intersections between the slice plane and triangle edges
        points = []
        for i in range(3):
            p1 = triangle[i]
            p2 = triangle[(i + 1) % 3]

            # Check if the edge crosses the slice height
            if (p1[2] <= height <= p2[2]) or (p2[2] <= height <= p1[2]):
                if p2[2] - p1[2] != 0:  # Avoid division by zero
                    # Calculate intersection point
                    t = (height - p1[2]) / (p2[2] - p1[2])
                    x = p1[0] + t * (p2[0] - p1[0])
                    y = p1[1] + t * (p2[1] - p1[1])

                    # Transform to image coordinates
                    img_x = int(x * scale + offset_x)
                    img_y = int(y * scale + offset_y)
                    points.append((img_x, img_y))

        # Draw line if we found two intersection points
        if len(points) == 2:
            draw.line(points, fill="black", width=2)

    # Save the image
    # img.save(output_path)
    # print(f"Slice saved to {output_path}")

    # Convert to numpy array and return
    return np.array(img, dtype=np.uint8)
