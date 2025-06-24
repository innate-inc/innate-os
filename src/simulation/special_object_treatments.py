"""
Special object treatments for occupancy grid generation.

This module provides custom handling for specific objects that require
non-standard representations in the occupancy grid.
"""

import numpy as np
from typing import Tuple, List, Optional
from scipy.spatial.transform import Rotation as R


class SpecialObjectHandler:
    """Handler for objects that need custom occupancy grid treatment."""

    def __init__(
        self,
        map_resolution: float,
        map_origin_x: float,
        map_origin_y: float,
        map_width: int,
        map_height: int,
    ):
        """
        Initialize the special object handler.

        Args:
            map_resolution: Resolution of the map in meters per cell
            map_origin_x: X origin of the map in world coordinates
            map_origin_y: Y origin of the map in world coordinates
            map_width: Width of the map in cells
            map_height: Height of the map in cells
        """
        self.map_resolution = map_resolution
        self.map_origin_x = map_origin_x
        self.map_origin_y = map_origin_y
        self.map_width = map_width
        self.map_height = map_height

    def world_to_grid(self, world_x: float, world_y: float) -> Tuple[int, int]:
        """Convert world coordinates to grid coordinates."""
        grid_x = int((world_x - self.map_origin_x) / self.map_resolution)
        grid_y = int((world_y - self.map_origin_y) / self.map_resolution)
        return grid_x, grid_y

    def is_valid_grid_cell(self, grid_x: int, grid_y: int) -> bool:
        """Check if grid coordinates are within bounds."""
        return 0 <= grid_x < self.map_width and 0 <= grid_y < self.map_height

    def apply_special_treatment(
        self,
        object_name: str,
        position: List[float],
        rotation: R,
        occupancy_grid: np.ndarray,
    ) -> bool:
        """
        Apply special treatment for an object if it has one.

        Args:
            object_name: Name of the object
            position: Position of the object [x, y, z]
            rotation: Rotation of the object as a scipy Rotation object
            occupancy_grid: The occupancy grid to modify

        Returns:
            True if special treatment was applied, False otherwise
        """
        # Check if this object has a special treatment
        if object_name == "frl_apartment_table_01":
            return self._handle_table_feet(position, rotation, occupancy_grid)

        # Add more special cases here as needed
        # elif object_name == "some_other_special_object":
        #     return self._handle_other_object(position, rotation, occupancy_grid)

        return False

    def _handle_table_feet(
        self, position: List[float], rotation: R, occupancy_grid: np.ndarray
    ) -> bool:
        """
        Handle the table by adding 4 small squares for the feet instead of a full rectangle.

        Args:
            position: Position of the table center [x, y, z]
            rotation: Rotation of the table
            occupancy_grid: The occupancy grid to modify

        Returns:
            True if treatment was successfully applied
        """
        try:
            # Table dimensions (estimated for a typical apartment table)
            # These could be made configurable or extracted from the actual model
            table_width = 0.8  # meters (width in X direction)
            table_depth = 1.5  # meters (depth in Y direction)
            foot_size = 0.12  # meters (size of each foot - 20cm square)

            # Foot positions relative to table center (before rotation)
            # Assuming feet are inset slightly from the edges
            inset = 0.05  # 5cm inset from edges
            foot_offset_x = table_width / 2 - inset
            foot_offset_y = table_depth / 2 - inset

            foot_positions_local = [
                [-foot_offset_x, -foot_offset_y, 0],  # Front-left
                [foot_offset_x, -foot_offset_y, 0],  # Front-right
                [-foot_offset_x, foot_offset_y, 0],  # Back-left
                [foot_offset_x, foot_offset_y, 0],  # Back-right
            ]

            # Transform foot positions to world coordinates
            table_center = np.array(position[:2])  # Only use X and Y

            for foot_pos_local in foot_positions_local:
                # Translate to world coordinates
                foot_world_pos = table_center + foot_pos_local[:2]

                # Convert to grid coordinates
                foot_grid_x, foot_grid_y = self.world_to_grid(
                    foot_world_pos[0], foot_world_pos[1]
                )

                # Calculate foot size in grid cells
                foot_size_cells = max(1, int(foot_size / self.map_resolution))
                half_foot_size = foot_size_cells // 2

                # Add the foot to the grid
                for dy in range(-half_foot_size, half_foot_size + 1):
                    for dx in range(-half_foot_size, half_foot_size + 1):
                        grid_x = foot_grid_x + dx
                        grid_y = foot_grid_y + dy

                        if self.is_valid_grid_cell(grid_x, grid_y):
                            occupancy_grid[grid_y, grid_x] = 255  # Mark as occupied

            print(f"Applied special table foot treatment for {position}")
            return True

        except Exception as e:
            print(f"Error applying table foot treatment: {e}")
            return False
