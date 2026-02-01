from math import atan, tan, radians, degrees
import queue
import numpy as np
import genesis as gs
import cv2  # for potential image saving/processing
import json
from scipy.spatial.transform import Rotation as R, Slerp
import time  # Add import for time functions
import os  # Add os import for path joining
import torch
from typing import Dict, Any  # Add typing imports

from src.simulation.stl_slicing import slice_stl
from src.simulation.special_object_treatments import SpecialObjectHandler
from src.agent.types import (
    RobotStateMsg,
    OccupancyGridMsg,
    VelocityCmd,
    ArmCmd,
    ArmGotoCmd,
    ArmStateMsg,
    PositionCmd,
    ResetRobotCmd,
    SetEnvironmentCmd,
)
from src.simulation.utils import rotate_vector
from src.shared_queues import SharedQueues


ROBOT_INIT_POS = (2, -5, 0.8)
ROBOT_INIT_QUAT = (0, 0, 0, 1)


def xyzw_to_wxyz(xyzw):
    return (xyzw[3], xyzw[0], xyzw[1], xyzw[2])


class SimulationNode:
    def __init__(self, shared_queues: SharedQueues, enable_vis: bool = True):
        self.shared_queues = shared_queues
        self.enable_vis = enable_vis
        self.loaded_entities = {}  # To store references to loaded entities
        self.loaded_dynamic_entities: Dict[str, gs.Entity] = {}
        self.managed_entities: Dict[str, gs.Entity] = {}
        self.env_config = None
        # Store trajectory data for managed entities
        self.entity_trajectories: Dict[str, Dict[str, Any]] = {}

        # Track dynamic entity positions for occupancy grid generation
        self.active_entities: Dict[str, Dict[str, Any]] = (
            {}
        )  # entity_name -> {position: [x, y, z], hitbox_type: "manual"/"aabb"}

        # Add timing variables for real-time simulation
        self.last_render_time = 0
        self.render_interval = 1 / 10  # Render at 15 FPS

        # Store commanded velocities for odometry
        self.commanded_lin_vel = np.zeros(3)  # [vx, vy, vz]
        self.commanded_ang_vel = np.zeros(3)  # [wx, wy, wz]

        # Navigation control parameters
        self.max_linear_velocity = 0.5  # m/s
        self.max_angular_velocity = 1.0  # rad/s
        self.position_tolerance = (
            0.02  # m - how close to target before considering reached
        )
        self.angle_tolerance = (
            0.02  # rad - how close to target angle before considering reached
        )

        # Current navigation target (None if no active target)
        self.nav_target_pos = None
        self.nav_target_yaw = None

        # Arm control state
        self.arm_joint_names = [
            "joint1",
            "joint2",
            "joint3",
            "joint4",
            "joint5",
            "joint6",
        ]
        self.arm_current_positions = [0.0] * 6  # Current joint positions
        self.arm_target_positions = None  # Target for interpolation
        self.arm_start_positions = None  # Start positions for interpolation
        self.arm_interpolation_start_time = None
        self.arm_interpolation_duration = None
        self.last_arm_state_time = 0
        self.arm_state_interval = 0.02  # Publish at ~50Hz

        self.render_camera_vfov = 40
        self.render_camera_hfov = degrees(
            2 * atan(tan(radians(self.render_camera_vfov) / 2) * 1280 / 720)
        )
        self.render_camera_res = (1280, 720)

        self.robot_camera_vfov = 80
        self.robot_camera_hfov = degrees(
            2 * atan(tan(radians(self.robot_camera_vfov) / 2) * 640 / 480)
        )
        self.robot_camera_res = (640, 480)

        print(
            f"Robot camera FOV (vfov, hfov, res): {self.robot_camera_vfov}, {self.robot_camera_hfov}, {self.robot_camera_res}"
        )

        # Initialize core components
        self._init_genesis()
        self._init_scene()
        self._init_environment()
        self._init_robot()
        self._init_camera()
        self._init_map_params()

        self.scene.build()

        print("Scene built")

        # Update occupancy grid with objects
        self._add_objects_to_occupancy_grid()

        self.init_movement()

        print("SimulationNode initialized.")

    def _init_genesis(self):
        """Initialize Genesis backend"""
        gs.init(backend=gs.gpu)

    def _init_scene(self):
        """Initialize the main simulation scene"""
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=0.02, substeps=10),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(3.5, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=self.render_camera_vfov,  # VERTICAL FOV
                res=self.render_camera_res,
            ),
            vis_options=gs.options.VisOptions(
                ambient_light=(0.5, 0.5, 0.5),
            ),
            show_FPS=False,
            show_viewer=self.enable_vis,
        )
        # Add ground plane
        self.scene.add_entity(gs.morphs.Plane(pos=(0, 0.05, 0)))

    def _init_environment(self):
        """Initialize the environment meshes and process occupancy grid"""
        # TODO: Make the base scene path configurable via env_config["environment_name"]
        base_scene_path = "data/ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb"
        base_scene_collision_config = "data/ReplicaCAD_baked_lighting/configs/stages/Baked_sc0_staging_00.stage_config.json"

        # Option 1: Load base scene without convexification
        # Prefix with _ to indicate it might be unused currently
        _replica_scene = self.scene.add_entity(
            gs.morphs.Mesh(
                file=base_scene_path,
                fixed=True,
                euler=(90, 0, 0),
                pos=(0, 0, 0),
                convexify=False,  # Load without convexification
                collision=False,  # No collision on main mesh
            )
        )

        # Initialize scene_objects list (for collision objects)
        self.scene_objects = []

        # Add separate collision geometry based on the stage config file
        self._add_collision_from_stage_config(base_scene_collision_config)

        # --- Pre-load all potential dynamic entities ---
        self._preload_dynamic_entities()

        # --- Apply an initial empty/default config to hide all preloaded entities initially? ---
        # Or handle this in _preload_dynamic_entities by placing them far away.
        # Let's place them far away during preload.

        # Export as STL (should probably only include static geometry here)
        # TODO: Revisit STL export - might not be needed or should exclude dynamic entities
        file_path = "data/replica_scene.stl"
        self._process_occupancy_grid(file_path)

    def _add_collision_from_stage_config(self, config_path):
        """Add collision geometry based on receptacles defined in the stage config"""
        with open(config_path, "r") as f:
            config = json.load(f)

        # Extract receptacles from user_defined section
        receptacles = config.get("user_defined", {})

        # Create rotation for 90 degrees around X-axis (same as euler=(90,0,0) in main scene)
        scene_rotation = R.from_euler("x", 90, degrees=True)

        # Add collision meshes for each receptacle
        for name, receptacle in receptacles.items():
            position = receptacle.get("position", [0, 0, 0])
            rotation = receptacle.get("rotation", [1, 0, 0, 0])

            # Convert position from list to numpy array
            position = np.array(position)

            # Rotate the position to match main scene orientation
            position = scene_rotation.apply(position)

            # Convert the original quaternion [w,x,y,z] to scipy format [x,y,z,w]
            original_quat = np.array(
                [rotation[1], rotation[2], rotation[3], rotation[0]]
            )
            original_rotation = R.from_quat(original_quat)

            # Combine rotations (scene rotation * original rotation)
            combined_rotation = scene_rotation * original_rotation

            # Convert back to [w,x,y,z] quaternion format for Genesis
            new_quat = combined_rotation.as_quat()  # [x,y,z,w]
            final_quat = [
                new_quat[3],
                new_quat[0],
                new_quat[1],
                new_quat[2],
            ]  # [w,x,y,z]

            # Extract object name from receptacle name
            if "_frl_apartment_" in name or "_frl_" in name:
                parts = name.split("_frl_")
                if len(parts) > 1:
                    # Extract the object name
                    object_name = "frl_" + parts[1]

                    # Remove any .001, .002, etc. suffixes from the object name
                    if "." in object_name:
                        base_name, suffix = object_name.rsplit(".", 1)
                        if suffix.isdigit() or (
                            len(suffix) > 0 and all(c.isdigit() for c in suffix)
                        ):
                            object_name = base_name

                    try:
                        # Load the actual object mesh with collision but no visualization
                        mesh_entity = self.scene.add_entity(
                            gs.morphs.Mesh(
                                file=f"data/ReplicaCAD_dataset/objects/{object_name}.glb",
                                pos=position.tolist(),
                                quat=final_quat,
                                fixed=True,
                                visualization=False,  # Invisible collision only
                                collision=True,
                                convexify=True,  # Individual objects can be safely convexified
                            )
                        )

                        # Store the entity reference for later AABB calculation
                        if not hasattr(self, "scene_objects"):
                            self.scene_objects = []

                        self.scene_objects.append(
                            {
                                "name": object_name,
                                "entity": mesh_entity,
                                "position": position,
                                "rotation": combined_rotation,
                            }
                        )

                        print(f"Added collision for: {object_name}")
                    except Exception as e:
                        print(f"Failed to load collision for {object_name}: {e}")

    def _process_occupancy_grid(self, file_path):
        """
        Process the STL mesh to create an occupancy grid.
        This version also retrieves the mesh bounds so that the map origin can be set
        based on the actual lower-left corner of the geometry.
        """
        slice_height_min = 0
        slice_height_max = slice_height_min + 0.20  # 20cm above the floor
        num_slices = 10
        self.base_occupancy_grid = None
        self.grid_bounds = None

        for height in np.linspace(slice_height_min, slice_height_max, num_slices):
            grid_slice, bounds = slice_stl(
                stl_path=file_path,
                height=height,
                output_path=f"replica_scene_sliced_{height:.1f}.png",
                pixel_size=0.05,
            )
            if self.base_occupancy_grid is None:
                self.base_occupancy_grid = grid_slice
                self.grid_bounds = bounds  # record bounds from the first slice
            else:
                self.base_occupancy_grid = np.minimum(
                    self.base_occupancy_grid, grid_slice, dtype=np.uint8
                )

        # Optionally, if needed, flip the grid along the vertical axis so (0,0) is bottom-left
        # self.base_occupancy_grid = np.flipud(self.base_occupancy_grid)

        # Initialize the grid with entities (what gets sent to agent)
        self.occupancy_grid_with_entities = self.base_occupancy_grid.copy()

    def _add_objects_to_occupancy_grid(self):
        """Add objects to the base occupancy grid based on their AABBs"""
        if not hasattr(self, "base_occupancy_grid") or self.base_occupancy_grid is None:
            print("Warning: Base occupancy grid not initialized yet")
            return

        if not hasattr(self, "scene_objects") or not self.scene_objects:
            print("No objects to add to occupancy grid")
            return

        print(f"Adding {len(self.scene_objects)} objects to base occupancy grid")

        # For each object, get its AABB and project onto the grid
        for obj in self.scene_objects:
            entity = obj["entity"]
            object_name = obj["name"]
            object_position = obj["position"]
            object_rotation = obj["rotation"]

            try:
                # Check if this object has a special treatment
                special_applied = self.special_object_handler.apply_special_treatment(
                    object_name,
                    object_position.tolist(),
                    object_rotation,
                    self.base_occupancy_grid,
                )

                if special_applied:
                    print(f"Applied special treatment for object {object_name}")
                    continue  # Skip the default AABB handling

                # Default AABB handling for objects without special treatment
                # Get the AABB (Axis-Aligned Bounding Box) of the mesh
                min_point, max_point = entity.get_AABB()

                # Convert to numpy arrays for easier manipulation
                min_point = np.array(min_point.cpu().numpy())
                max_point = np.array(max_point.cpu().numpy())

                # We only care about x and y for the occupancy grid (floor plan)
                # Convert world coordinates to grid coordinates
                min_grid_x = int(
                    (min_point[0] - self.map_origin_x) / self.map_resolution
                )
                min_grid_y = int(
                    (min_point[1] - self.map_origin_y) / self.map_resolution
                )
                max_grid_x = int(
                    (max_point[0] - self.map_origin_x) / self.map_resolution
                )
                max_grid_y = int(
                    (max_point[1] - self.map_origin_y) / self.map_resolution
                )

                # Ensure coordinates are within grid bounds
                min_grid_x = max(0, min(min_grid_x, self.map_width - 1))
                min_grid_y = max(0, min(min_grid_y, self.map_height - 1))
                max_grid_x = max(0, min(max_grid_x, self.map_width - 1))
                max_grid_y = max(0, min(max_grid_y, self.map_height - 1))

                print(
                    f"Adding object {obj['name']} to occupancy grid: ({min_grid_x}, {min_grid_y}) to ({max_grid_x}, {max_grid_y})"
                )

                # Fill the bounding box with occupied cells (255 = occupied in this grid)
                for y in range(min_grid_y, max_grid_y + 1):
                    for x in range(min_grid_x, max_grid_x + 1):
                        if 0 <= y < self.map_height and 0 <= x < self.map_width:
                            self.base_occupancy_grid[y, x] = 255

                print(
                    f"Added object {obj['name']} to occupancy grid: ({min_grid_x}, {min_grid_y}) to ({max_grid_x}, {max_grid_y})"
                )

            except Exception as e:
                print(f"Failed to add {obj['name']} to occupancy grid: {e}")

        self._save_occupancy_grid_debug("_with_static_objects")

        # Initialize the grid with entities (what gets sent to agent)
        self.occupancy_grid_with_entities = self.base_occupancy_grid.copy()

    def _regenerate_occupancy_grid_with_entities(self):
        """
        Regenerate the occupancy grid with entities from base grid + all active entities.
        This replaces the old add/remove approach with a clean regeneration.
        """
        # Start with a fresh copy of the base grid (static environment only)
        self.occupancy_grid_with_entities = self.base_occupancy_grid.copy()

        # Add all currently active entities
        for entity_name, entity_info in self.active_entities.items():
            position = entity_info["position"]

            # Add this entity's hitbox to the entities grid
            self._add_single_entity_to_grid(entity_name, position)

        print(f"Regenerated occupancy grid with {len(self.active_entities)} entities")
        # Mark grid as changed for immediate republishing
        self.occupancy_grid_changed = True

        # Save debug grids whenever entities change
        self._save_occupancy_grid_debug("_with_entities")

    def _add_single_entity_to_grid(self, entity_name: str, position: list):
        """
        Add a single entity's hitbox to the CURRENT occupancy grid.
        This method assumes the grid is already initialized and ready for entity addition.
        """
        if entity_name not in self.managed_entities:
            print(f"Warning: Entity {entity_name} not found in managed entities")
            return

        entity_obj = self.managed_entities[entity_name]

        try:
            # Check if manual hitbox is defined for this entity
            if entity_name in self.manual_entity_hitboxes:
                # Use manual hitbox definition (centered on entity position)
                manual_hitbox = self.manual_entity_hitboxes[entity_name]
                width_m = manual_hitbox["width"]
                height_m = manual_hitbox["height"]

                # Convert entity position to grid coordinates
                center_grid_x = int(
                    (position[0] - self.map_origin_x) / self.map_resolution
                )
                center_grid_y = int(
                    (position[1] - self.map_origin_y) / self.map_resolution
                )

                # Convert hitbox size from meters to grid cells
                width_cells = int(width_m / self.map_resolution)
                height_cells = int(height_m / self.map_resolution)

                # Calculate hitbox bounds (centered on entity position)
                half_width = width_cells // 2
                half_height = height_cells // 2

                min_grid_x = center_grid_x - half_width
                max_grid_x = center_grid_x + half_width
                min_grid_y = center_grid_y - half_height
                max_grid_y = center_grid_y + half_height

            else:
                # Get the actual AABB (Axis-Aligned Bounding Box) of the entity
                min_point, max_point = entity_obj.get_AABB()

                # Convert to numpy arrays for easier manipulation
                min_point = np.array(min_point.cpu().numpy())
                max_point = np.array(max_point.cpu().numpy())

                # Project the 3D bounding box onto the 2D occupancy grid (use only X and Y)
                # Convert world coordinates to grid coordinates
                min_grid_x = int(
                    (min_point[0] - self.map_origin_x) / self.map_resolution
                )
                min_grid_y = int(
                    (min_point[1] - self.map_origin_y) / self.map_resolution
                )
                max_grid_x = int(
                    (max_point[0] - self.map_origin_x) / self.map_resolution
                )
                max_grid_y = int(
                    (max_point[1] - self.map_origin_y) / self.map_resolution
                )

            # Ensure coordinates are within grid bounds
            min_grid_x = max(0, min(min_grid_x, self.map_width - 1))
            min_grid_y = max(0, min(min_grid_y, self.map_height - 1))
            max_grid_x = max(0, min(max_grid_x, self.map_width - 1))
            max_grid_y = max(0, min(max_grid_y, self.map_height - 1))

            print(
                f"Adding entity {entity_name} to occupancy grid: ({min_grid_x}, {min_grid_y}) to ({max_grid_x}, {max_grid_y})"
            )

            # Fill the bounding box area with occupied cells (0 = occupied, 255 = free)
            for y in range(min_grid_y, max_grid_y + 1):
                for x in range(min_grid_x, max_grid_x + 1):
                    if 0 <= y < self.map_height and 0 <= x < self.map_width:
                        self.occupancy_grid_with_entities[y, x] = (
                            255  # Mark as occupied
                        )

        except Exception as e:
            print(f"Failed to add entity {entity_name} to grid: {e}")

    def _add_entity_to_active_list(self, entity_name: str, position: list):
        """
        Add or update an entity in the active entities list and regenerate the occupancy grid.
        This replaces the old grid-manipulation approach with a clean regeneration system.
        """
        if entity_name not in self.managed_entities:
            print(f"Warning: Entity {entity_name} not found in managed entities")
            return

        # Determine hitbox type for logging
        hitbox_type = "manual" if entity_name in self.manual_entity_hitboxes else "aabb"

        # Store/update entity info
        self.active_entities[entity_name] = {
            "position": position.copy(),
            "hitbox_type": hitbox_type,
        }

        print(
            f"Added/updated entity {entity_name} at {position} (hitbox: {hitbox_type})"
        )

        # Regenerate the entire occupancy grid with all entities
        self._regenerate_occupancy_grid_with_entities()

    def _clear_all_active_entities(self):
        """Remove all dynamic entities from the active list and regenerate the occupancy grid"""
        entity_count = len(self.active_entities)
        self.active_entities.clear()
        print(f"Cleared {entity_count} entities from active list")

        if entity_count > 0:
            # Regenerate the occupancy grid (will be just the base grid now)
            self._regenerate_occupancy_grid_with_entities()

    def _save_occupancy_grid_debug(self, filename_suffix: str = ""):
        """Save both base and entities grids for debugging purposes"""
        if self.base_occupancy_grid is not None:
            base_filename = f"base_occupancy_grid{filename_suffix}.png"
            cv2.imwrite(base_filename, self.base_occupancy_grid)
            print(f"Saved base occupancy grid to {base_filename}")

        if (
            hasattr(self, "occupancy_grid_with_entities")
            and self.occupancy_grid_with_entities is not None
        ):
            entities_filename = f"occupancy_grid_with_entities{filename_suffix}.png"
            cv2.imwrite(entities_filename, self.occupancy_grid_with_entities)
            print(f"Saved entities occupancy grid to {entities_filename}")

    def _init_robot(self):
        """Initialize robot and its parameters"""
        self.robot = self.scene.add_entity(
            gs.morphs.URDF(
                file="data/urdf/maurice.urdf",
                pos=ROBOT_INIT_POS,
                quat=xyzw_to_wxyz(ROBOT_INIT_QUAT),
                fixed=True,  # Disable physics dynamics - robot is moved kinematically via set_pos/set_quat
            )
        )

    def _apply_arm_positions(self, joint_positions):
        """Apply joint positions to arm and update current state.

        Args:
            joint_positions: List of 6 floats [j0, j1, j2, j3, j4, j5] in radians
        """
        # Get DOF indices for all arm joints
        dof_indices = []
        positions = []
        for i, joint_name in enumerate(self.arm_joint_names):
            if i < len(joint_positions):
                try:
                    joint = self.robot.get_joint(joint_name)
                    dof_idx = joint.dofs_idx_local
                    if dof_idx is not None and len(dof_idx) > 0:
                        dof_indices.append(dof_idx[0])
                        positions.append(joint_positions[i])
                        self.arm_current_positions[i] = joint_positions[i]
                except Exception as e:
                    print(f"[SimulationNode] Error getting joint {joint_name}: {e}")

        # Also add the mirrored gripper finger (joint6M mirrors joint6)
        if len(joint_positions) >= 6:
            try:
                joint6m = self.robot.get_joint("joint6M")
                dof_idx = joint6m.dofs_idx_local
                if dof_idx is not None and len(dof_idx) > 0:
                    dof_indices.append(dof_idx[0])
                    positions.append(-joint_positions[5])  # Mimic with -1 multiplier
            except Exception as e:
                print(f"[SimulationNode] Error getting joint6M: {e}")

        # Apply all positions at once using robot entity
        if dof_indices and positions:
            try:
                self.robot.set_dofs_position(
                    position=torch.tensor(positions, dtype=torch.float32),
                    dofs_idx_local=dof_indices,
                    zero_velocity=True,
                )
            except Exception as e:
                print(f"[SimulationNode] Error applying arm positions: {e}")

    def _init_camera(self):
        """Initialize robot camera, arm wrist camera, and chase camera"""
        # Main robot camera (will be positioned at arm_camera_link)
        self.robot_camera = self.scene.add_camera(
            res=self.robot_camera_res,
            pos=(0, 0, 0),
            lookat=(1, 0, 0),
            fov=self.robot_camera_vfov,
        )

        # Arm wrist camera (mounted on link5, looking forward along the arm)
        self.arm_wrist_camera = self.scene.add_camera(
            res=self.robot_camera_res,
            pos=(0, 0, 0),
            lookat=(1, 0, 0),
            fov=self.robot_camera_vfov,
        )

        # Add chase camera
        self.chase_camera = self.scene.add_camera(
            res=self.render_camera_res,
            pos=(0, -2.0, 2.0),  # 2m behind, 2m up
            lookat=(0, 0, 0),  # Will be updated to track robot
            fov=self.render_camera_vfov,
        )

        # Camera intrinsics (for the robot camera)
        self.width = 640
        self.height = 480
        self.fx = 554.256
        self.fy = 554.256
        self.cx = 320.0
        self.cy = 240.0

    def _init_map_params(self):
        """
        Initialize mapping parameters using the bounds extracted from the STL file.
        Now, (0,0) of the grid (cell [0, 0]) corresponds to (min_x, min_y) in world coordinates.
        """
        self.map_width = self.base_occupancy_grid.shape[1]  # columns
        self.map_height = self.base_occupancy_grid.shape[0]  # rows
        self.map_resolution = self.grid_bounds["pixel_size"]
        self.map_origin_x = self.grid_bounds["min_x"]
        self.map_origin_y = self.grid_bounds["min_y"]
        self.map_publish_interval = 10
        self.last_map_publish_step = 0
        self.occupancy_grid_changed = (
            False  # Flag to track when grid needs republishing
        )

        # Initialize special object handler
        self.special_object_handler = SpecialObjectHandler(
            map_resolution=self.map_resolution,
            map_origin_x=self.map_origin_x,
            map_origin_y=self.map_origin_y,
            map_width=self.map_width,
            map_height=self.map_height,
        )

    def init_movement(self):
        """Initialize robot movement - no-op for Maurice"""
        pass

    def _update_navigation_movement(self, dt):
        """Smoothly move robot towards navigation target with velocity limits."""
        # Get current robot state
        current_pos = self.robot.get_pos().cpu().numpy()
        current_quat = self.robot.get_quat().cpu().numpy()  # [w, x, y, z]

        # Convert current quaternion to yaw angle
        current_yaw = 2 * np.arctan2(
            current_quat[3], current_quat[0]
        )  # z, w components

        # Calculate position error
        pos_error = self.nav_target_pos - current_pos
        distance_to_target = np.linalg.norm(pos_error[:2])  # Only X,Y distance

        # Calculate angle error (normalize to [-pi, pi])
        angle_error = self.nav_target_yaw - current_yaw
        while angle_error > np.pi:
            angle_error -= 2 * np.pi
        while angle_error < -np.pi:
            angle_error += 2 * np.pi

        # Check if we've reached the target
        position_reached = distance_to_target < self.position_tolerance
        angle_reached = abs(angle_error) < self.angle_tolerance

        if position_reached and angle_reached:
            # Target reached, clear navigation target
            self.nav_target_pos = None
            self.nav_target_yaw = None
            self.commanded_lin_vel = np.zeros(3)
            self.commanded_ang_vel = np.zeros(3)
            return

        # Calculate desired velocities
        if distance_to_target > self.position_tolerance:
            # Calculate direction to target (in world frame)
            direction = pos_error[:2] / distance_to_target

            # Apply velocity limit
            desired_speed = min(self.max_linear_velocity, distance_to_target / dt)

            # Calculate velocity in world frame
            linear_velocity = direction * desired_speed

            # Move robot position
            new_pos = current_pos + np.array(
                [linear_velocity[0] * dt, linear_velocity[1] * dt, 0]
            )
            self.robot.set_pos(new_pos)

            # Store commanded velocities for odometry
            self.commanded_lin_vel = np.array(
                [linear_velocity[0], linear_velocity[1], 0]
            )
        else:
            self.commanded_lin_vel = np.zeros(3)

        # Handle rotation
        if abs(angle_error) > self.angle_tolerance:
            # Apply angular velocity limit
            desired_angular_vel = np.sign(angle_error) * min(
                self.max_angular_velocity, abs(angle_error) / dt
            )

            # Update orientation
            new_yaw = current_yaw + desired_angular_vel * dt

            # Convert yaw to quaternion [w, x, y, z]
            new_quat = [
                np.cos(new_yaw / 2.0),  # w
                0.0,  # x
                0.0,  # y
                np.sin(new_yaw / 2.0),  # z
            ]

            self.robot.set_quat(new_quat)

            # Store commanded angular velocity for odometry
            self.commanded_ang_vel = np.array([0, 0, desired_angular_vel])
        else:
            self.commanded_ang_vel = np.zeros(3)

    def _apply_environment_config(self, config: Dict[str, Any]):
        """Activates and positions managed entities based on config, hides others."""
        print(
            "[SimulationNode] Applying environment configuration "
            "via entity placement..."
        )

        # Clear previous trajectory data and active entities
        self.entity_trajectories.clear()
        self._clear_all_active_entities()

        if not self.managed_entities:
            print(
                "[SimulationNode] No managed entities were pre-loaded. "
                "Cannot apply config."
            )
            return

        # Set of entity names specified in the current config
        active_entity_names = set()
        if config and "entities" in config:
            for entity_data in config["entities"]:
                name = entity_data.get("name")
                if name:
                    active_entity_names.add(name)

        # Iterate through all potentially active entities defined in the config
        if config and "entities" in config:
            for entity_data in config["entities"]:
                name = entity_data.get("name")
                poses = entity_data.get("poses", [])
                # Get loop parameter, default to False
                loop = entity_data.get("loop", False)

                if name not in self.managed_entities:
                    print(
                        f"[SimulationNode] Warning: Entity '{name}' in config "
                        f"was not pre-loaded. Skipping."
                    )
                    continue

                entity_obj = self.managed_entities[name]

                # Handle fixed entities (single pose)
                if len(poses) == 1:
                    pose = poses[0]
                    position = pose.get("position")
                    orientation = pose.get("orientation")  # Assumed [w, x, y, z]

                    if position is None or orientation is None:
                        print(
                            f"[SimulationNode] Skipping entity '{name}': missing "
                            f"pos/orient in pose: {pose}"
                        )
                        # Move it out of the way just in case
                        entity_obj.set_pos([0, 0, -1000])
                        continue

                    print(f"[SimulationNode] Placing entity: '{name}' at {position}")
                    try:
                        entity_obj.set_pos(position)
                        entity_obj.set_quat(orientation)  # set_quat uses w,x,y,z

                        # Add entity to active list for fixed entities
                        self._add_entity_to_active_list(name, position)

                    except Exception as e:
                        print(f"[SimulationNode] Error placing entity '{name}': {e}")

                elif len(poses) > 1:
                    # Store trajectory data for update loop
                    # Sort poses by time just in case they aren't ordered
                    sorted_poses = sorted(poses, key=lambda p: p["time"])
                    self.entity_trajectories[name] = {
                        "poses": sorted_poses,
                        "loop": loop,
                    }
                    # Initial placement will be handled by the update loop
                    print(
                        f"[SimulationNode] Trajectory defined for '{name}'. Initial pose set by run loop."
                    )

                else:
                    print(
                        f"[SimulationNode] Skipping entity '{name}': no poses defined."
                    )
                    # Move it out of the way
                    entity_obj.set_pos([0, 0, -1000])

        # Note: Debug grids are automatically saved when entities regenerate

    def _update_entity_poses(self, sim_time: float):
        """Updates positions of entities based on their trajectories and sim_time."""
        for name, trajectory_data in self.entity_trajectories.items():
            if name not in self.managed_entities:
                continue  # Should not happen, but safety check

            entity_obj = self.managed_entities[name]
            poses = trajectory_data["poses"]
            loop = trajectory_data["loop"]

            if not poses:
                continue

            # Determine current segment based on time
            current_time = sim_time
            start_pose = poses[0]
            end_pose = poses[-1]
            trajectory_duration = end_pose["time"] - start_pose["time"]

            if loop and trajectory_duration > 1e-6:  # Avoid division by zero
                if current_time >= end_pose["time"]:
                    # Wrap time for looping trajectory
                    current_time = (
                        current_time - start_pose["time"]
                    ) % trajectory_duration + start_pose["time"]

            # Find the two keyframes surrounding the current time
            p1 = None
            p2 = None
            for i in range(len(poses) - 1):
                if poses[i]["time"] <= current_time < poses[i + 1]["time"]:
                    p1 = poses[i]
                    p2 = poses[i + 1]
                    break

            # Handle edge cases: before first pose or after last pose (non-looping)
            if p1 is None:
                if current_time < poses[0]["time"]:
                    p1 = p2 = poses[0]  # Stay at first pose
                else:  # After last pose (and not looping or duration is zero)
                    p1 = p2 = poses[-1]  # Stay at last pose

            # Calculate interpolation factor (alpha)
            segment_duration = p2["time"] - p1["time"]
            alpha = 0.0
            if segment_duration > 1e-6:
                alpha = (current_time - p1["time"]) / segment_duration
            alpha = np.clip(alpha, 0.0, 1.0)  # Clamp between 0 and 1

            # Interpolate position (LERP)
            pos1 = np.array(p1["position"])
            pos2 = np.array(p2["position"])
            interp_pos = pos1 + alpha * (pos2 - pos1)

            # Interpolate orientation (SLERP)
            # Need orientations in [x, y, z, w] format for Slerp
            # Input quat is [w, x, y, z]
            quat1_wxyz = p1["orientation"]
            quat2_wxyz = p2["orientation"]
            quat1_xyzw = np.array(
                [quat1_wxyz[1], quat1_wxyz[2], quat1_wxyz[3], quat1_wxyz[0]]
            )
            quat2_xyzw = np.array(
                [quat2_wxyz[1], quat2_wxyz[2], quat2_wxyz[3], quat2_wxyz[0]]
            )

            try:
                key_rots = R.from_quat([quat1_xyzw, quat2_xyzw])
                slerp = Slerp([p1["time"], p2["time"]], key_rots)
                interp_rot = slerp([current_time])[0]  # Slerp expects array of times
                # Convert back to [w, x, y, z] for Genesis
                interp_quat_xyzw = interp_rot.as_quat()
                interp_quat_wxyz = [
                    interp_quat_xyzw[3],
                    interp_quat_xyzw[0],
                    interp_quat_xyzw[1],
                    interp_quat_xyzw[2],
                ]
            except Exception as e:
                # Fallback to p1 orientation if slerp fails (e.g., identical quats)
                # print(f"SLERP failed for {name} at time {current_time}: {e}. "
                #       f"Using start orientation.")
                interp_quat_wxyz = quat1_wxyz

            # Check if position changed significantly to update occupancy grid
            position_changed = True
            if name in self.active_entities:
                old_pos = self.active_entities[name]["position"]
                # Check if position changed by more than half a grid cell
                pos_diff = np.linalg.norm(np.array(interp_pos) - np.array(old_pos))
                if pos_diff < self.map_resolution * 0.5:
                    position_changed = False

            # Apply interpolated pose (scale is already set)
            entity_obj.set_pos(interp_pos.tolist())
            entity_obj.set_quat(interp_quat_wxyz)

            # Update entity position and regenerate occupancy grid if moved significantly
            if position_changed:
                # Update entity position in active list (this will regenerate the grid)
                self._add_entity_to_active_list(name, interp_pos.tolist())

    def _preload_dynamic_entities(self):
        """Pre-loads all known dynamic entities into the scene initially."""
        print("[SimulationNode] Pre-loading dynamic entities...")

        # Define potential entities here (name, path, default scale, optional manual hitbox)
        potential_entities = [
            {
                "name": "walker_1",
                "asset_path": "data/assets/walking_man/man.obj",
                "scale": [1.0, 1.0, 1.0],
                "hitbox": {
                    "width": 0.6,
                    "height": 0.6,
                },  # Optional manual hitbox (meters)
            },
            {
                "name": "casualty_1",
                "asset_path": "data/assets/lying_man/Lying_man_0127.obj",
                "scale": [0.010, 0.010, 0.010],
                "hitbox": {"width": 0.6, "height": 2.0},
            },
            # {
            #     "name": "banana_peel",
            #     "asset_path": "data/assets/palatial_asset_bef5/bef5.xml",
            #     "scale": 1.0,
            # },
            # Add other potential entities here in the future
        ]

        initial_hide_pos = [0, 0, -1000]  # Position to hide entities initially
        default_quat = [1.0, 0.0, 0.0, 0.0]  # Default orientation (w,x,y,z)

        # Extract manual hitboxes from entity definitions
        self.manual_entity_hitboxes = {}
        for entity_data in potential_entities:
            if "hitbox" in entity_data:
                self.manual_entity_hitboxes[entity_data["name"]] = entity_data["hitbox"]

        for entity_data in potential_entities:
            name = entity_data["name"]
            asset_path = entity_data["asset_path"]
            scale = entity_data["scale"]
            print(f"[SimulationNode] Pre-loading: {name} from {asset_path}")

            try:
                # Construct absolute asset path relative to project root
                project_root = os.path.dirname(
                    os.path.dirname(os.path.dirname(__file__))
                )
                full_asset_path = os.path.join(project_root, asset_path)

                entity_obj = self.scene.add_entity(
                    gs.morphs.Mesh(
                        file=full_asset_path,
                        pos=initial_hide_pos,  # Start hidden
                        quat=default_quat,
                        scale=scale,
                        collision=False,
                        convexify=False,
                    )
                    # if asset_path.endswith((".obj", ".glb", ".gltf", ".stl"))
                    # else gs.morphs.MJCF(
                    #     file=full_asset_path,
                    #     pos=initial_hide_pos,  # Start hidden
                    #     quat=default_quat,
                    #     scale=scale,
                    #     collision=False,
                    #     convexify=False,
                    #     visualization=True,
                    #     requires_jac_and_IK=True,
                    # )
                )
                # Store reference
                self.managed_entities[name] = entity_obj
                print(f"[SimulationNode] Pre-loaded '{name}' successfully.")
            except Exception as e:
                print(
                    f"[SimulationNode] Error pre-loading entity '{name}' from "
                    f"path '{asset_path}': {e}"
                )

    def run(self):
        local_forward = np.array([1.0, 0.0, 0.0])
        step_count = 0
        sim_time = 0  # Track simulation time

        # Get the simulation timestep from the scene
        dt = self.scene.sim_options.dt
        last_step_time = time.time()

        while not self.shared_queues.exit_event.is_set():
            # --- (A) Handle velocity commands from agent -> sim first
            try:
                # Process all messages in queue and keep track of latest commands
                latest_velocity_cmd = None
                latest_position_cmd = None
                latest_reset_cmd = None
                latest_set_env_cmd = None  # Variable to hold the latest env command
                latest_arm_cmd = None
                latest_arm_goto_cmd = None

                while True:
                    try:
                        cmd = self.shared_queues.agent_to_sim.get_nowait()
                        if isinstance(cmd, VelocityCmd):
                            latest_velocity_cmd = cmd
                        elif isinstance(cmd, ArmCmd):
                            latest_arm_cmd = cmd
                        elif isinstance(cmd, ArmGotoCmd):
                            latest_arm_goto_cmd = cmd
                        elif isinstance(cmd, PositionCmd):
                            latest_position_cmd = cmd
                        elif isinstance(cmd, ResetRobotCmd):
                            latest_reset_cmd = cmd
                        elif isinstance(
                            cmd, SetEnvironmentCmd
                        ):  # Check for new command
                            latest_set_env_cmd = cmd
                    except queue.Empty:
                        break

                # Apply latest SetEnvironmentCmd FIRST if it exists
                if latest_set_env_cmd is not None:
                    print("[SimulationNode] Received SetEnvironmentCmd.")
                    self._apply_environment_config(latest_set_env_cmd.config)
                    # We might want to reset robot pose after env change, TBD

                # Apply latest ResetRobotCmd if it exists (after potential env change)
                if latest_reset_cmd is not None:
                    if (
                        hasattr(latest_reset_cmd, "pose")
                        and latest_reset_cmd.pose is not None
                    ):
                        # Use custom position and orientation
                        custom_position, custom_orientation = latest_reset_cmd.pose
                        print(
                            f"[SimulationNode] Resetting robot to custom pose: "
                            f"pos={custom_position}, quat={xyzw_to_wxyz(custom_orientation)} (w, x, y, z)"
                        )
                        self.robot.set_pos(custom_position)
                        self.robot.set_quat(xyzw_to_wxyz(custom_orientation))
                    else:
                        # Use default position and orientation
                        print(
                            "[SimulationNode] Resetting robot pose to default origin."
                        )
                        self.robot.set_pos(ROBOT_INIT_POS)
                        self.robot.set_quat(xyzw_to_wxyz(ROBOT_INIT_QUAT))

                    # Reset commanded velocities
                    self.commanded_lin_vel = np.zeros(3)
                    self.commanded_ang_vel = np.zeros(3)

                # Apply arm joint positions if we have an arm command (immediate)
                if latest_arm_cmd is not None:
                    joint_positions = latest_arm_cmd.joint_positions
                    print(f"[SimulationNode] Applying arm positions: {joint_positions}")
                    self._apply_arm_positions(joint_positions)
                    # Cancel any ongoing interpolation
                    self.arm_target_positions = None

                # Start arm interpolation if we have a goto command
                if latest_arm_goto_cmd is not None:
                    self.arm_start_positions = self.arm_current_positions.copy()
                    self.arm_target_positions = latest_arm_goto_cmd.joint_positions
                    self.arm_interpolation_start_time = sim_time
                    self.arm_interpolation_duration = max(
                        0.1, latest_arm_goto_cmd.duration
                    )

                # Update arm interpolation if active
                if self.arm_target_positions is not None:
                    elapsed = sim_time - self.arm_interpolation_start_time
                    t = min(1.0, elapsed / self.arm_interpolation_duration)
                    # Smooth interpolation using ease-in-out
                    t_smooth = t * t * (3 - 2 * t)
                    interpolated = [
                        self.arm_start_positions[i]
                        + t_smooth
                        * (self.arm_target_positions[i] - self.arm_start_positions[i])
                        for i in range(6)
                    ]
                    self._apply_arm_positions(interpolated)
                    if t >= 1.0:
                        self.arm_target_positions = None  # Done interpolating
                else:
                    # Continuously apply current positions to maintain arm pose (torque control)
                    self._apply_arm_positions(self.arm_current_positions)

                if latest_position_cmd is not None:
                    # Set navigation target for smooth movement
                    self.nav_target_pos = np.array(
                        [
                            latest_position_cmd.target_x,
                            latest_position_cmd.target_y,
                            latest_position_cmd.target_z,
                        ]
                    )
                    self.nav_target_yaw = latest_position_cmd.target_yaw

                    # print(f"New nav target: pos=({self.nav_target_pos[0]:.3f}, {self.nav_target_pos[1]:.3f}), yaw={self.nav_target_yaw:.3f}")
                elif latest_velocity_cmd is not None:
                    linear_vel = latest_velocity_cmd.linear_x
                    angular_vel = latest_velocity_cmd.angular_z

                    # Convert desired velocities to robot position update
                    current_pos = self.robot.get_pos().cpu().numpy()
                    current_quat = self.robot.get_quat().cpu().numpy()

                    # Update position based on linear velocity and current orientation
                    dt = self.scene.sim_options.dt
                    # Convert current quaternion to rotation matrix to get forward direction
                    current_rot = R.from_quat(
                        [
                            current_quat[1],
                            current_quat[2],
                            current_quat[3],
                            current_quat[0],
                        ]
                    )
                    forward_dir = current_rot.apply(
                        [1, 0, 0]
                    )  # Transform x-axis by current rotation

                    # Move in the forward direction
                    new_pos = current_pos + forward_dir * linear_vel * dt

                    # Update orientation based on angular velocity
                    angle = angular_vel * dt
                    delta_rot = R.from_euler("z", angle)
                    new_rot = delta_rot * current_rot
                    new_quat = new_rot.as_quat()  # Returns [x, y, z, w]
                    new_quat = np.array(
                        [new_quat[3], new_quat[0], new_quat[1], new_quat[2]]
                    )  # Convert to Genesis format

                    # Set the new position and orientation
                    self.robot.set_pos(new_pos)
                    self.robot.set_quat(new_quat)

                    # Store commanded velocities in world frame for odometry
                    # MULTIPLY BY 0 TO DISABLE MESSING WITH TH EACTUAL ROBOT
                    # ON ROS
                    self.commanded_lin_vel = forward_dir * linear_vel * 0
                    self.commanded_ang_vel = np.array([0, 0, angular_vel])

                    print(
                        f"Commanded vel: linear={linear_vel:.3f}, angular={angular_vel:.3f}"
                    )
            except Exception as e:
                print(f"Error processing commands: {e}")

            # --- (B) Handle smooth navigation movement ---
            if self.nav_target_pos is not None and self.nav_target_yaw is not None:
                self._update_navigation_movement(dt)

            # --- (C) Update moving entities ---
            self._update_entity_poses(sim_time)

            # --- (D) Gather robot pose, velocity (after applying commands)
            pos = self.robot.get_pos().cpu().numpy()
            quat = self.robot.get_quat().cpu().numpy()

            # Use commanded velocities for odometry
            lin_vel = self.commanded_lin_vel
            ang_vel = self.commanded_ang_vel

            # --- (E) Render cameras based on time interval
            sim_time += dt

            # --- Publish Clock Message
            sec_clock = int(sim_time)
            nsec_clock = int((sim_time - sec_clock) * 1e9)
            clock_msg = {"clock": {"sec": sec_clock, "nanosec": nsec_clock}}
            try:
                self.shared_queues.sim_to_agent.put_nowait(clock_msg)
            except queue.Full:
                pass

            # Check if enough time has passed since last render
            if sim_time - self.last_render_time >= self.render_interval:
                camera_link = self.robot.get_link("head_camera_link")
                camera_pos = camera_link.get_pos()
                camera_quat = camera_link.get_quat()
                look_dir = rotate_vector(local_forward, camera_quat)
                lookat = camera_pos.cpu().numpy() + look_dir
                self.robot_camera.set_pose(pos=camera_pos.cpu().numpy(), lookat=lookat)

                # Update arm wrist camera (mounted on link5, looking forward along the arm)
                link5 = self.robot.get_link("link5")
                link5_pos = link5.get_pos()
                link5_quat = link5.get_quat()
                arm_forward = np.array([1.0, 0.0, 0.0])  # Arm points along X axis
                arm_look_dir = rotate_vector(arm_forward, link5_quat)
                arm_lookat = link5_pos.cpu().numpy() + arm_look_dir
                self.arm_wrist_camera.set_pose(
                    pos=link5_pos.cpu().numpy(), lookat=arm_lookat
                )

                # Update chase camera to follow robot
                robot_pos = self.robot.get_pos().cpu().numpy()
                robot_quat = self.robot.get_quat().cpu().numpy()
                offset = np.array([-2.0, 0.0, 2.0])  # 2m behind, 2m up
                rotated_offset = rotate_vector(offset, robot_quat)
                chase_pos = robot_pos + rotated_offset
                self.chase_camera.set_pose(pos=chase_pos, lookat=robot_pos)

                # Render all cameras
                rgb, depth, seg, normal = self.robot_camera.render(depth=True)
                arm_rgb, _, _, _ = self.arm_wrist_camera.render()
                chase_rgb, _, _, _ = self.chase_camera.render()

                # Convert RGB to BGR format if needed (or BGR to RGB)
                if rgb is not None:
                    rgb_to_send = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                else:
                    rgb_to_send = None

                if arm_rgb is not None:
                    arm_rgb_to_send = cv2.cvtColor(arm_rgb, cv2.COLOR_RGB2BGR)
                else:
                    arm_rgb_to_send = None

                if chase_rgb is not None:
                    chase_rgb = cv2.cvtColor(chase_rgb, cv2.COLOR_RGB2BGR)

                depth_to_send = depth

                try:
                    camera_views = {
                        "first_person": rgb_to_send,
                        "arm_wrist": arm_rgb_to_send,
                        "chase": chase_rgb,
                    }
                    self.shared_queues.sim_to_web.put_nowait(camera_views)
                except queue.Full:
                    pass

                # Update last render time
                self.last_render_time = sim_time
            else:
                rgb_to_send = None
                depth_to_send = None
                arm_rgb_to_send = None

            # --- (E2) Publish arm joint state at ~50Hz
            if sim_time - self.last_arm_state_time >= self.arm_state_interval:
                arm_state_msg = ArmStateMsg(
                    joint_positions=self.arm_current_positions.copy(),
                    joint_names=self.arm_joint_names,
                )
                try:
                    self.shared_queues.sim_to_agent.put_nowait(arm_state_msg)
                except queue.Full:
                    pass
                self.last_arm_state_time = sim_time

            # --- (F) Build and publish RobotStateMsg with latest state
            state_msg = RobotStateMsg(
                # camera data
                rgb_frame=rgb_to_send if "rgb_to_send" in locals() else None,
                depth_frame=depth_to_send if "depth_to_send" in locals() else None,
                arm_rgb_frame=(
                    arm_rgb_to_send if "arm_rgb_to_send" in locals() else None
                ),
                # camera intrinsics
                width=self.width,
                height=self.height,
                fx=self.fx,
                fy=self.fy,
                cx=self.cx,
                cy=self.cy,
                frame_id="camera_color_frame",
                distortion_model="plumb_bob",
                D=[0.0, 0.0, 0.0, 0.0, 0.0],
                # odometry: pose
                px=pos[0],
                py=pos[1],
                pz=pos[2],
                ox=quat[1],
                oy=quat[2],
                oz=quat[3],
                ow=quat[0],
                # odometry: velocity
                vx=lin_vel[0],
                vy=lin_vel[1],
                vz=lin_vel[2],
                wx=ang_vel[0],
                wy=ang_vel[1],
                wz=ang_vel[2],
            )

            # Update shared robot position and orientation directly (more reliable than waiting for websocket bridge)
            self.shared_queues.update_robot_pose(
                pos[0], pos[1], pos[2], quat[1], quat[2], quat[3], quat[0], sim_time
            )

            # Publish the unified RobotStateMsg to the bridge
            try:
                self.shared_queues.sim_to_agent.put_nowait(state_msg)
            except queue.Full:
                pass

            # --- (G) Publish the map when changed or periodically
            should_publish_map = (
                self.occupancy_grid_changed
                or (step_count - self.last_map_publish_step)
                >= self.map_publish_interval
            )

            if should_publish_map:
                # Build OccupancyGridMsg with current state (including entities)
                og_msg = OccupancyGridMsg(
                    width=self.map_width,
                    height=self.map_height,
                    resolution=self.map_resolution,
                    origin_x=self.map_origin_x,
                    origin_y=self.map_origin_y,
                    origin_z=0.0,
                    origin_yaw=0.0,
                    data=self.occupancy_grid_with_entities,
                    frame_id="map",
                )
                try:
                    self.shared_queues.sim_to_agent.put_nowait(og_msg)
                except queue.Full:
                    pass

                # Reset flags
                self.last_map_publish_step = step_count
                self.occupancy_grid_changed = False

            # --- (H) Step the physics
            try:
                self.scene.step()
                step_count += 1

                # --- (I) Sleep to maintain real-time simulation
                current_time = time.time()
                elapsed = current_time - last_step_time
                sleep_time = dt - elapsed

                if sleep_time > 0:
                    time.sleep(sleep_time)
                elif sleep_time < -0.1:  # Only warn if we're significantly behind
                    print(
                        f"[SimulationNode] Warning: Simulation running slower than real-time (behind by {-sleep_time:.3f}s)"
                    )

                # Update last step time for next iteration
                last_step_time = time.time()

            except Exception as e:
                if "Viewer closed" in str(e):
                    print("Viewer closed, stopping simulation.")
                    self.shared_queues.exit_event.set()
                    break
                else:
                    print(f"Error in SimulationNode: {e}")
                    self.shared_queues.exit_event.set()
                    break

        # Cleanup
        if self.enable_vis:
            self.scene.viewer.stop()
        print("SimulationNode stopped.")
