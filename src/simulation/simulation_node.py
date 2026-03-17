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
from typing import Dict, Any, List, Optional  # Add typing imports

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
    DrawTrajectoryCmd,
    ClearTrajectoryCmd,
)
from src.simulation.utils import rotate_vector
from src.shared_queues import SharedQueues


ROBOT_INIT_POS = (2, -5, 0.05)
ROBOT_INIT_QUAT = (0, 0, 0, 1)

DEFAULT_SCENE_CONFIG = {
    "name": "Baked_sc0_staging_00",
    "mesh_path": "data/ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb",
    "mesh_euler": [90, 0, 0],
    "collision_stage_config": "data/ReplicaCAD_baked_lighting/configs/stages/Baked_sc0_staging_00.stage_config.json",
    "occupancy_stl_path": "data/replica_scene.stl",
    "slice_output_prefix": "replica_scene_sliced",
}

SCENE_PRESETS = {
    "Baked_sc0_staging_00": DEFAULT_SCENE_CONFIG,
}


class EnvironmentRebuildRequired(Exception):
    """Raised when applying an environment requires rebuilding the Genesis scene."""


def xyzw_to_wxyz(xyzw):
    return (xyzw[3], xyzw[0], xyzw[1], xyzw[2])


class SimulationNode:
    def __init__(
        self,
        shared_queues: SharedQueues,
        enable_vis: bool = True,
        initial_env_config: Optional[Dict[str, Any]] = None,
        robot_collision_enabled: bool = True,
    ):
        self.shared_queues = shared_queues
        self.enable_vis = enable_vis
        self.robot_collision_enabled = robot_collision_enabled
        self.loaded_entities = {}  # To store references to loaded entities
        self.loaded_dynamic_entities: Dict[str, gs.Entity] = {}
        self.managed_entities: Dict[str, gs.Entity] = {}
        self.env_config = initial_env_config
        self.project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self.default_entity_catalog: Dict[str, Dict[str, Any]] = {}
        self.entity_specs: Dict[str, Dict[str, Any]] = {}
        self.manual_entity_hitboxes: Dict[str, Dict[str, float]] = {}
        self.current_scene_config = self._resolve_scene_config(initial_env_config)
        self.scene_built = False
        self.current_entity_asset_signature: Optional[tuple[tuple[str, str], ...]] = (
            None
        )
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

        # Navigation control parameters - differential drive kinematics
        # Physical constraints:
        self.wheel_base = 0.18  # m - distance between wheels (from URDF collision box)
        self.max_wheel_speed = 0.6  # m/s - max individual wheel speed
        # Derived limits: max_linear = max_wheel_speed, max_angular = 2*max_wheel_speed/wheel_base
        # Software caps (for smoother motion, can be at or below physical limits):
        self.linear_velocity_cap = 0.5  # m/s - software cap on forward speed
        self.angular_velocity_cap = 1.0  # rad/s - software cap on rotation speed
        # Tolerances:
        self.position_tolerance = (
            0.02  # m - how close to target before considering reached
        )
        self.angle_tolerance = (
            0.02  # rad - how close to target angle before considering reached
        )
        # Heading must be within this angle of movement direction before moving forward
        self.heading_alignment_threshold = 0.15  # rad (~8.6 degrees)

        # Current navigation target (None if no active target)
        self.nav_target_pos = None
        self.nav_target_yaw = None

        # Trajectory visualization state
        self.trajectory_debug_objects = (
            []
        )  # Store references to debug objects for clearing

        # Logical robot position tracking (separate from physics mesh to avoid collision drift)
        # These track the "intended" robot position, unaffected by collision resolution
        self.robot_logical_pos = np.array(ROBOT_INIT_POS)
        self.robot_logical_yaw = 0.0  # Extracted from ROBOT_INIT_QUAT

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
        self.scene_built = True

        print("Scene built")

        # Update occupancy grid with objects
        self._add_objects_to_occupancy_grid()

        self.init_movement()

        if self.env_config:
            print("[SimulationNode] Applying initial environment configuration...")
            self._apply_environment_config(self.env_config, allow_rebuild=False)
        else:
            # Explicitly represent empty active entity set at startup.
            self.current_entity_asset_signature = tuple()

        print("SimulationNode initialized.")

    def _init_genesis(self):
        """Initialize Genesis backend"""
        gs.init(backend=gs.cpu)

    def _init_scene(self):
        """Initialize the main simulation scene"""
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=0.05, substeps=4),
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

    def _resolve_project_path(self, path: str) -> str:
        """Resolve relative paths from project root while preserving absolute paths."""
        if os.path.isabs(path):
            return path
        return os.path.join(self.project_root, path)

    def _resolve_scene_config(
        self, env_config: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Resolve static scene settings from an environment config."""
        scene_config = DEFAULT_SCENE_CONFIG.copy()
        scene_config["mesh_euler"] = list(DEFAULT_SCENE_CONFIG["mesh_euler"])

        env_name = None
        if env_config and env_config.get("environment_name"):
            env_name = env_config["environment_name"]
            preset = SCENE_PRESETS.get(env_name)
            if preset:
                scene_config.update(preset)
                scene_config["mesh_euler"] = list(preset.get("mesh_euler", [90, 0, 0]))
            else:
                scene_config["name"] = env_name

        scene_block = env_config.get("scene") if isinstance(env_config, dict) else None
        if isinstance(scene_block, dict):
            scene_config["name"] = scene_block.get(
                "name", env_name or scene_config["name"]
            )
            if scene_block.get("mesh_path"):
                scene_config["mesh_path"] = scene_block["mesh_path"]
            if scene_block.get("mesh_euler") is not None:
                scene_config["mesh_euler"] = scene_block["mesh_euler"]
            if "collision_stage_config" in scene_block:
                scene_config["collision_stage_config"] = scene_block.get(
                    "collision_stage_config"
                )
            if scene_block.get("occupancy_stl_path"):
                scene_config["occupancy_stl_path"] = scene_block["occupancy_stl_path"]
            if scene_block.get("slice_output_prefix"):
                scene_config["slice_output_prefix"] = scene_block["slice_output_prefix"]
        elif env_name:
            scene_config["name"] = env_name

        return scene_config

    def _init_environment(self):
        """Initialize static scene geometry and occupancy baseline."""
        scene_config = self.current_scene_config
        base_scene_path = self._resolve_project_path(scene_config["mesh_path"])
        base_scene_euler = tuple(scene_config.get("mesh_euler", [90, 0, 0]))

        print(
            f"[SimulationNode] Loading static scene '{scene_config['name']}' "
            f"from {base_scene_path}"
        )

        self.scene.add_entity(
            gs.morphs.Mesh(
                file=base_scene_path,
                fixed=True,
                euler=base_scene_euler,
                pos=(0, 0, 0),
                convexify=False,
                collision=False,
                file_meshes_are_zup=False,  # genesis 0.4.x defaults to True for GLB; preserve manual euler
            )
        )

        # Initialize scene_objects list (for static collision objects)
        self.scene_objects = []

        # Add separate collision geometry when a stage config is available.
        collision_stage_config = scene_config.get("collision_stage_config")
        if collision_stage_config:
            self._add_collision_from_stage_config(
                self._resolve_project_path(collision_stage_config),
                scene_euler=base_scene_euler,
            )
        else:
            print(
                "[SimulationNode] No collision_stage_config configured; "
                "skipping static collision mesh import."
            )

        # Register default entity metadata and load startup-requested entities.
        self._preload_dynamic_entities()

        # Pre-load entities referenced by the startup environment config before build().
        startup_entity_errors = self._ensure_config_entities_loaded(self.env_config)
        if startup_entity_errors:
            details = "; ".join(
                f"{name}: {error}" for name, error in startup_entity_errors.items()
            )
            raise RuntimeError(
                "Failed to load startup environment entities before scene build: "
                f"{details}"
            )

        self._process_occupancy_grid(
            self._resolve_project_path(scene_config["occupancy_stl_path"]),
            output_prefix=scene_config.get("slice_output_prefix", "scene_sliced"),
        )

    def _add_collision_from_stage_config(
        self, config_path: str, scene_euler: tuple = (90, 0, 0)
    ):
        """Add collision geometry based on receptacles defined in the stage config"""
        if not os.path.exists(config_path):
            print(
                f"[SimulationNode] Collision config not found at {config_path}; "
                "skipping."
            )
            return

        with open(config_path, "r") as f:
            config = json.load(f)

        # Extract receptacles from user_defined section
        receptacles = config.get("user_defined", {})

        # Align receptacle transforms with the configured static scene orientation.
        scene_rotation = R.from_euler("xyz", scene_euler, degrees=True)

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
                                file=self._resolve_project_path(
                                    f"data/ReplicaCAD_dataset/objects/{object_name}.glb"
                                ),
                                pos=position.tolist(),
                                quat=final_quat,
                                fixed=True,
                                visualization=False,  # Invisible collision only
                                collision=True,
                                convexify=True,  # Individual objects can be safely convexified
                                file_meshes_are_zup=False,  # genesis 0.4.x: preserve pre-0.4 rotation behavior
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

    def _process_occupancy_grid(
        self, file_path: str, output_prefix: str = "scene_slice"
    ):
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

        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"[SimulationNode] Occupancy STL not found at path: {file_path}"
            )

        for height in np.linspace(slice_height_min, slice_height_max, num_slices):
            grid_slice, bounds = slice_stl(
                stl_path=file_path,
                height=height,
                output_path=f"{output_prefix}_{height:.1f}.png",
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
        urdf_kwargs = {
            "file": "data/urdf/maurice.urdf",
            "pos": ROBOT_INIT_POS,
            "quat": xyzw_to_wxyz(ROBOT_INIT_QUAT),
            # Disable physics dynamics - robot is moved kinematically via set_pos/set_quat.
            "fixed": True,
        }
        if not self.robot_collision_enabled:
            # Temporary troubleshooting mode: ghost robot through scene geometry.
            urdf_kwargs["collision"] = False
            print("[SimulationNode] Robot collisions disabled.")

        self.robot = self.scene.add_entity(gs.morphs.URDF(**urdf_kwargs))

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

    def _update_navigation_movement(self, dt, sim_time=0.0):
        """
        Differential drive navigation: robot heading must be tangent to trajectory.

        Key constraints:
        1. Robot can only move forward/backward in the direction it's facing
        2. Must rotate to face target before moving forward
        3. Speed is limited by differential drive kinematics:
           - v_left = v - ω * L/2
           - v_right = v + ω * L/2
           - Both wheel speeds must be <= max_wheel_speed

        NOTE: Uses logical position tracking to avoid drift from collision resolution.
        The physics mesh may be pushed by collisions, but navigation calculations
        use the tracked logical position to maintain consistent reference frame.
        """
        # Use tracked logical position instead of physics mesh position
        # This prevents collision-induced drift from affecting navigation
        current_pos = self.robot_logical_pos.copy()
        current_yaw = self.robot_logical_yaw

        # Calculate position error
        pos_error = self.nav_target_pos - current_pos
        distance_to_target = np.linalg.norm(pos_error[:2])  # Only X,Y distance

        # Calculate the angle TO the target position (direction we need to move)
        angle_to_target = np.arctan2(pos_error[1], pos_error[0])

        # Calculate heading error: difference between current heading and direction to target
        heading_error = angle_to_target - current_yaw
        # Normalize to [-pi, pi]
        while heading_error > np.pi:
            heading_error -= 2 * np.pi
        while heading_error < -np.pi:
            heading_error += 2 * np.pi

        # Calculate final orientation error (for when we reach position)
        final_angle_error = self.nav_target_yaw - current_yaw
        while final_angle_error > np.pi:
            final_angle_error -= 2 * np.pi
        while final_angle_error < -np.pi:
            final_angle_error += 2 * np.pi

        # Check if we've reached the target position
        position_reached = distance_to_target < self.position_tolerance
        final_angle_reached = abs(final_angle_error) < self.angle_tolerance

        if position_reached and final_angle_reached:
            # Target fully reached, clear navigation target
            self.nav_target_pos = None
            self.nav_target_yaw = None
            self.commanded_lin_vel = np.zeros(3)
            self.commanded_ang_vel = np.zeros(3)
            return

        # Determine what to do based on state
        linear_vel = 0.0
        angular_vel = 0.0

        if not position_reached:
            # We need to move toward the target
            # Check if heading is aligned with movement direction
            heading_aligned = abs(heading_error) < self.heading_alignment_threshold

            if heading_aligned:
                # Heading is aligned - move forward while making small corrections
                # Calculate desired angular velocity for path correction
                angular_vel = np.clip(
                    heading_error / dt,
                    -self.angular_velocity_cap,
                    self.angular_velocity_cap,
                )

                # Calculate max linear velocity given angular velocity (differential drive constraint)
                # |v| + |ω| * L/2 <= max_wheel_speed
                max_linear_for_turn = (
                    self.max_wheel_speed - abs(angular_vel) * self.wheel_base / 2
                )
                max_linear_for_turn = max(0.0, max_linear_for_turn)  # Can't be negative

                # Also limit by software cap and distance (don't overshoot)
                desired_linear = min(
                    self.linear_velocity_cap,
                    max_linear_for_turn,
                    distance_to_target / dt,
                )

                linear_vel = desired_linear
            else:
                # Heading not aligned - rotate in place to face target
                angular_vel = np.sign(heading_error) * min(
                    self.angular_velocity_cap,
                    abs(heading_error) / dt,
                )
                linear_vel = 0.0  # Don't move forward while rotating significantly
        else:
            # Position reached, rotate to final orientation
            angular_vel = np.sign(final_angle_error) * min(
                self.angular_velocity_cap,
                abs(final_angle_error) / dt,
            )
            linear_vel = 0.0

        # Apply movement using differential drive model
        # Robot moves in the direction it's facing
        cos_yaw = np.cos(current_yaw)
        sin_yaw = np.sin(current_yaw)

        # Update position (forward motion in heading direction)
        new_pos = current_pos + np.array(
            [cos_yaw * linear_vel * dt, sin_yaw * linear_vel * dt, 0.0]
        )

        # Update orientation
        new_yaw = current_yaw + angular_vel * dt
        new_quat = [
            np.cos(new_yaw / 2.0),  # w
            0.0,  # x
            0.0,  # y
            np.sin(new_yaw / 2.0),  # z
        ]

        # Update logical position tracking (this is our source of truth)
        self.robot_logical_pos = new_pos.copy()
        self.robot_logical_yaw = new_yaw

        # Apply to physics mesh for visualization (may be pushed by collisions,
        # but we don't read it back for navigation calculations)
        self.robot.set_pos(new_pos)
        self.robot.set_quat(new_quat)

        # Store commanded velocities for odometry (in world frame)
        self.commanded_lin_vel = np.array(
            [cos_yaw * linear_vel, sin_yaw * linear_vel, 0]
        )
        self.commanded_ang_vel = np.array([0, 0, angular_vel])

    def _clear_trajectory_visualization(self):
        """Remove all previously drawn trajectory debug objects from the scene."""
        if not self.trajectory_debug_objects:
            return

        print(
            f"[SimulationNode] Clearing {len(self.trajectory_debug_objects)} trajectory objects"
        )
        for obj in self.trajectory_debug_objects:
            try:
                if obj is not None:
                    self.scene.clear_debug_object(obj)
            except Exception as e:
                print(
                    f"[SimulationNode] Warning: Failed to remove trajectory object: {e}"
                )
        self.trajectory_debug_objects.clear()

    def _catmull_rom_spline(self, points, num_segments=10):
        """
        Generate a smooth Catmull-Rom spline through the given points.
        Returns interpolated points along the curve.

        Args:
            points: List of (x, y) tuples representing waypoints
            num_segments: Number of interpolated segments between each pair of points
        """
        if len(points) < 2:
            return points

        result = []

        # Pad the points for Catmull-Rom (duplicate first and last points)
        padded = [points[0]] + points + [points[-1]]

        for i in range(1, len(padded) - 2):
            p0 = np.array(padded[i - 1])
            p1 = np.array(padded[i])
            p2 = np.array(padded[i + 1])
            p3 = np.array(padded[i + 2])

            for t in np.linspace(0, 1, num_segments, endpoint=False):
                # Catmull-Rom spline formula
                t2 = t * t
                t3 = t2 * t

                point = 0.5 * (
                    (2 * p1)
                    + (-p0 + p2) * t
                    + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                    + (-p0 + 3 * p1 - 3 * p2 + p3) * t3
                )
                result.append(tuple(point))

        # Add the last point
        result.append(points[-1])

        return result

    def _draw_trajectory(self, cmd: DrawTrajectoryCmd):
        """
        Draw a navigation trajectory on the floor as a smooth curve.

        Args:
            cmd: DrawTrajectoryCmd containing waypoints and visualization parameters
        """
        if not cmd.waypoints or len(cmd.waypoints) < 2:
            print("[SimulationNode] Cannot draw trajectory: need at least 2 waypoints")
            return

        # Clear any existing trajectory
        self._clear_trajectory_visualization()

        # Extract (x, y) points from waypoints
        points_2d = [(wp.x, wp.y) for wp in cmd.waypoints]

        # Interpolate using Catmull-Rom spline for smooth curve
        # Use more segments for longer paths
        segments_per_waypoint = max(5, min(20, 50 // len(points_2d)))
        interpolated_points = self._catmull_rom_spline(
            points_2d, num_segments=segments_per_waypoint
        )

        print(
            f"[SimulationNode] Drawing trajectory: {len(cmd.waypoints)} waypoints -> "
            f"{len(interpolated_points)} interpolated points"
        )

        # Draw line segments between interpolated points
        floor_z = cmd.floor_height
        for i in range(len(interpolated_points) - 1):
            start = (interpolated_points[i][0], interpolated_points[i][1], floor_z)
            end = (
                interpolated_points[i + 1][0],
                interpolated_points[i + 1][1],
                floor_z,
            )

            try:
                obj = self.scene.draw_debug_line(
                    start=start,
                    end=end,
                    radius=cmd.line_radius,
                    color=cmd.color,
                )
                if obj is not None:
                    self.trajectory_debug_objects.append(obj)
            except Exception as e:
                print(f"[SimulationNode] Error drawing trajectory segment: {e}")
                break

        # Draw small spheres at original waypoints for visibility
        waypoint_color = (1.0, 0.5, 0.0, 0.9)  # Orange for waypoints
        for i, wp in enumerate(cmd.waypoints):
            try:
                # Slightly larger sphere for start/end
                radius = 0.04 if (i == 0 or i == len(cmd.waypoints) - 1) else 0.025
                obj = self.scene.draw_debug_sphere(
                    pos=(wp.x, wp.y, floor_z + 0.01),
                    radius=radius,
                    color=(
                        waypoint_color if i > 0 else (0.0, 1.0, 0.0, 0.9)
                    ),  # Green for start
                )
                if obj is not None:
                    self.trajectory_debug_objects.append(obj)
            except Exception as e:
                print(f"[SimulationNode] Error drawing waypoint sphere: {e}")

        print(
            f"[SimulationNode] Trajectory visualization complete: {len(self.trajectory_debug_objects)} objects"
        )

    def _normalize_scale(self, scale: Any) -> List[float]:
        """Normalize scale values to Genesis-compatible xyz list."""
        if isinstance(scale, (int, float)):
            uniform = float(scale)
            return [uniform, uniform, uniform]

        if isinstance(scale, (list, tuple)) and len(scale) == 3:
            return [float(scale[0]), float(scale[1]), float(scale[2])]

        return [1.0, 1.0, 1.0]

    def _load_dynamic_entity(
        self,
        name: str,
        asset_path: str,
        scale: Any = None,
        hitbox: Optional[Dict[str, float]] = None,
    ) -> tuple[bool, Optional[str]]:
        """Load a dynamic entity mesh into the scene and keep it hidden by default."""
        if name in self.managed_entities:
            return True, None

        normalized_scale = self._normalize_scale(scale)
        full_asset_path = self._resolve_project_path(asset_path)

        if not os.path.exists(full_asset_path):
            error = f"Cannot load '{name}': asset path not found ({full_asset_path})"
            print(f"[SimulationNode] {error}")
            return False, error

        if self.scene_built:
            error = (
                f"Cannot load '{name}' from '{full_asset_path}': "
                "scene is already built."
            )
            print(f"[SimulationNode] {error}")
            return False, error

        initial_pos = [0.0, 0.0, 0.0]
        default_quat = [1.0, 0.0, 0.0, 0.0]

        try:
            entity_obj = self.scene.add_entity(
                gs.morphs.Mesh(
                    file=full_asset_path,
                    pos=initial_pos,
                    quat=default_quat,
                    scale=normalized_scale,
                    collision=False,
                    convexify=False,
                    file_meshes_are_zup=False,  # genesis 0.4.x: preserve pre-0.4 rotation behavior
                )
            )
            self.managed_entities[name] = entity_obj
            self.entity_specs[name] = {
                "asset_path": asset_path,
                "scale": normalized_scale,
            }
            if hitbox is not None:
                self.manual_entity_hitboxes[name] = hitbox

            print(
                f"[SimulationNode] Loaded dynamic entity '{name}' from "
                f"{full_asset_path}"
            )
            return True, None
        except Exception as e:
            error = f"Error loading entity '{name}' from path '{full_asset_path}': {e}"
            print(f"[SimulationNode] {error}")
            return False, error

    def _ensure_config_entities_loaded(
        self, config: Optional[Dict[str, Any]]
    ) -> Dict[str, str]:
        """Load any entities referenced in config that are not yet managed."""
        if not config or "entities" not in config:
            return {}

        load_errors: Dict[str, str] = {}

        for entity_data in config["entities"]:
            name = entity_data.get("name")
            if not name:
                continue

            if name in self.managed_entities:
                existing_spec = self.entity_specs.get(name, {})
                requested_asset = entity_data.get("asset_path")
                if (
                    requested_asset
                    and existing_spec
                    and requested_asset != existing_spec.get("asset_path")
                ):
                    error = (
                        f"Entity '{name}' already loaded from "
                        f"{existing_spec.get('asset_path')} and cannot be hot-swapped "
                        f"to {requested_asset} at runtime."
                    )
                    print(f"[SimulationNode] {error}")
                    load_errors[name] = error
                # Allow per-config hitbox overrides for already loaded entities.
                hitbox = entity_data.get("hitbox")
                if hitbox is not None:
                    self.manual_entity_hitboxes[name] = hitbox
                continue

            default_spec = self.default_entity_catalog.get(name, {})
            asset_path = entity_data.get("asset_path", default_spec.get("asset_path"))
            scale = entity_data.get("scale", default_spec.get("scale", [1.0, 1.0, 1.0]))
            hitbox = entity_data.get("hitbox", default_spec.get("hitbox"))

            if not asset_path:
                error = (
                    f"Skipping entity '{name}': missing asset_path "
                    "and no default entry exists."
                )
                print(f"[SimulationNode] {error}")
                load_errors[name] = error
                continue

            loaded, error = self._load_dynamic_entity(
                name=name,
                asset_path=asset_path,
                scale=scale,
                hitbox=hitbox,
            )
            if not loaded:
                load_errors[name] = error or "Unknown entity load failure."

        return load_errors

    def _is_static_scene_change_requested(
        self, config: Optional[Dict[str, Any]]
    ) -> bool:
        """Check if config requests a different static scene than current runtime."""
        requested_scene = self._resolve_scene_config(config)
        keys = (
            "mesh_path",
            "mesh_euler",
            "collision_stage_config",
            "occupancy_stl_path",
        )
        for key in keys:
            if requested_scene.get(key) != self.current_scene_config.get(key):
                return True
        return False

    def _requires_rebuild_for_load_errors(self, load_errors: Dict[str, str]) -> bool:
        """Return True when load errors are caused by built-scene constraints."""
        if not load_errors:
            return False
        return all("scene is already built" in error for error in load_errors.values())

    def _build_requested_entity_asset_signature(
        self, config: Optional[Dict[str, Any]]
    ) -> tuple[tuple[str, str], ...]:
        """Canonical signature of requested entity->asset mappings."""
        if not config:
            return tuple()

        signature_items: List[tuple[str, str]] = []
        for entity_data in config.get("entities", []):
            name = entity_data.get("name")
            if not name:
                continue

            explicit_asset = entity_data.get("asset_path")
            existing_asset = self.entity_specs.get(name, {}).get("asset_path")
            default_asset = self.default_entity_catalog.get(name, {}).get("asset_path")
            resolved_asset = explicit_asset or existing_asset or default_asset or ""
            signature_items.append((name, resolved_asset))

        signature_items.sort()
        return tuple(signature_items)

    def _rebuild_scene_for_environment(self, config: Dict[str, Any]) -> None:
        """Tear down and rebuild the scene for a new static world/entity set."""
        print("[SimulationNode] Rebuilding scene for new environment...")
        self.env_config = config
        self.current_scene_config = self._resolve_scene_config(config)

        # Clear runtime state tied to old scene objects.
        self.managed_entities.clear()
        self.entity_specs.clear()
        self.default_entity_catalog.clear()
        self.manual_entity_hitboxes.clear()
        self.entity_trajectories.clear()
        self.active_entities.clear()
        self.current_entity_asset_signature = None
        self.loaded_entities.clear()
        self.loaded_dynamic_entities.clear()
        self.scene_objects = []
        self.trajectory_debug_objects = []

        self.nav_target_pos = None
        self.nav_target_yaw = None
        self.commanded_lin_vel = np.zeros(3)
        self.commanded_ang_vel = np.zeros(3)

        # Destroy old scene if API exposes a destroy method.
        if hasattr(self, "scene") and self.scene is not None:
            try:
                if hasattr(self.scene, "destroy"):
                    self.scene.destroy()
            except Exception as e:
                print(f"[SimulationNode] Warning: scene.destroy() failed: {e}")

        self.scene_built = False

        self._init_scene()
        self._init_environment()
        self._init_robot()
        self._init_camera()
        self._init_map_params()

        self.scene.build()
        self.scene_built = True
        print("[SimulationNode] Scene rebuilt")

        self._add_objects_to_occupancy_grid()
        self.init_movement()

        # Reset logical robot tracking to known default after rebuild.
        self.robot_logical_pos = np.array(ROBOT_INIT_POS)
        self.robot_logical_yaw = 0.0

        # Apply requested placements/trajectories onto the rebuilt scene.
        self._apply_environment_config(config, allow_rebuild=False)

    def _apply_environment_config(
        self, config: Dict[str, Any], allow_rebuild: bool = True
    ):
        """Load (or rebuild for) requested assets, then place configured entities."""
        print(
            "[SimulationNode] Applying environment configuration "
            "via entity placement..."
        )

        self.env_config = config

        if self._is_static_scene_change_requested(config):
            requested_scene = self._resolve_scene_config(config)
            if allow_rebuild:
                raise EnvironmentRebuildRequired(
                    "Static scene change requested from "
                    f"'{self.current_scene_config['name']}' to '{requested_scene['name']}', "
                    "scene rebuild required."
                )
            raise RuntimeError(
                "Static scene change requested from "
                f"'{self.current_scene_config['name']}' to '{requested_scene['name']}', "
                "but scene rebuild is disabled for this apply path."
            )

        requested_signature = self._build_requested_entity_asset_signature(config)
        if (
            allow_rebuild
            and self.current_entity_asset_signature is not None
            and requested_signature != self.current_entity_asset_signature
        ):
            raise EnvironmentRebuildRequired(
                "Requested entity/asset set differs from currently active environment. "
                "Scene rebuild required."
            )

        load_errors = self._ensure_config_entities_loaded(config)
        if load_errors:
            details = "; ".join(
                f"{name}: {error}" for name, error in load_errors.items()
            )
            if allow_rebuild and self._requires_rebuild_for_load_errors(load_errors):
                raise EnvironmentRebuildRequired(
                    "Environment introduces entities that are not in the current "
                    "built scene. Scene rebuild required. "
                    f"Details: {details}"
                )
            raise RuntimeError(
                "Failed to load requested environment entities. " f"Details: {details}"
            )

        # Clear previous trajectory data and active entities
        self.entity_trajectories.clear()
        self._clear_all_active_entities()

        entities = config.get("entities", []) if config else []
        if not entities:
            print(
                "[SimulationNode] No entities in environment config. "
                "Cleared active entities."
            )
            self.current_entity_asset_signature = requested_signature
            return

        if not self.managed_entities:
            raise RuntimeError(
                "No managed entities are loaded, but environment requested entities."
            )

        # Iterate through all potentially active entities defined in the config
        if entities:
            for entity_data in entities:
                name = entity_data.get("name")
                poses = entity_data.get("poses", [])
                # Get loop parameter, default to False
                loop = entity_data.get("loop", False)

                if name not in self.managed_entities:
                    raise RuntimeError(
                        f"Entity '{name}' is not loaded; cannot apply environment."
                    )

                entity_obj = self.managed_entities[name]

                # Handle fixed entities (single pose)
                if len(poses) == 1:
                    pose = poses[0]
                    position = pose.get("position")
                    orientation = pose.get("orientation")  # Assumed [w, x, y, z]

                    if position is None or orientation is None:
                        raise RuntimeError(
                            f"Entity '{name}' is missing position/orientation in pose: "
                            f"{pose}"
                        )

                    print(f"[SimulationNode] Placing entity: '{name}' at {position}")
                    try:
                        entity_obj.set_pos(position)
                        entity_obj.set_quat(orientation)  # set_quat uses w,x,y,z

                        # Add entity to active list for fixed entities
                        self._add_entity_to_active_list(name, position)

                    except Exception as e:
                        raise RuntimeError(f"Error placing entity '{name}': {e}") from e

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
                    raise RuntimeError(f"Entity '{name}' has no poses defined.")

        self.current_entity_asset_signature = requested_signature
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
        """
        Register default entity metadata used as fallbacks in environment configs.
        Actual entity loading is driven by the target environment config.
        """
        print("[SimulationNode] Registering default entity catalog...")

        default_entities = [
            {
                "name": "walker_1",
                "asset_path": "data/assets/walking_man/man.obj",
                "scale": [1.0, 1.0, 1.0],
                "hitbox": {"width": 0.6, "height": 0.6},
            },
            {
                "name": "casualty_1",
                "asset_path": "data/assets/lying_man/Lying_man_0127.obj",
                "scale": [0.010, 0.010, 0.010],
                "hitbox": {"width": 0.6, "height": 2.0},
            },
        ]

        self.default_entity_catalog = {
            entity_data["name"]: entity_data.copy() for entity_data in default_entities
        }

    def _report_set_environment_result(
        self, cmd: SetEnvironmentCmd, success: bool, error: Optional[str] = None
    ) -> None:
        """Send set_environment apply status back to API layer."""
        self.shared_queues.set_environment_apply_result(
            request_id=getattr(cmd, "request_id", None),
            success=success,
            error=error,
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
                        elif isinstance(cmd, DrawTrajectoryCmd):
                            self._draw_trajectory(cmd)
                        elif isinstance(cmd, ClearTrajectoryCmd):
                            self._clear_trajectory_visualization()
                    except queue.Empty:
                        break

                # Apply latest SetEnvironmentCmd FIRST if it exists
                if latest_set_env_cmd is not None:
                    print("[SimulationNode] Received SetEnvironmentCmd.")
                    try:
                        self._apply_environment_config(latest_set_env_cmd.config)
                    except EnvironmentRebuildRequired as rebuild_exc:
                        print(
                            "[SimulationNode] Environment requires rebuild: "
                            f"{rebuild_exc}"
                        )
                        try:
                            self._rebuild_scene_for_environment(
                                latest_set_env_cmd.config
                            )
                            print(
                                "[SimulationNode] Environment rebuild/apply complete."
                            )
                            self._report_set_environment_result(
                                latest_set_env_cmd, success=True
                            )
                        except Exception as e:
                            error = f"Failed to rebuild environment: {e}"
                            print(f"[SimulationNode] {error}")
                            self._report_set_environment_result(
                                latest_set_env_cmd, success=False, error=error
                            )
                    except Exception as e:
                        error = str(e)
                        print(f"[SimulationNode] Failed to apply environment: {error}")
                        self._report_set_environment_result(
                            latest_set_env_cmd, success=False, error=error
                        )
                    else:
                        self._report_set_environment_result(
                            latest_set_env_cmd, success=True
                        )

                    # Scene may have been rebuilt with different sim options.
                    dt = self.scene.sim_options.dt
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
                        # Update logical position tracking
                        self.robot_logical_pos = np.array(custom_position)
                        # Extract yaw from quaternion (xyzw format)
                        qx, qy, qz, qw = custom_orientation
                        self.robot_logical_yaw = np.arctan2(
                            2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz)
                        )
                    else:
                        # Use default position and orientation
                        print(
                            "[SimulationNode] Resetting robot pose to default origin."
                        )
                        self.robot.set_pos(ROBOT_INIT_POS)
                        self.robot.set_quat(xyzw_to_wxyz(ROBOT_INIT_QUAT))
                        # Update logical position tracking
                        self.robot_logical_pos = np.array(ROBOT_INIT_POS)
                        self.robot_logical_yaw = 0.0  # Default orientation has yaw=0

                    # Reset commanded velocities
                    self.commanded_lin_vel = np.zeros(3)
                    self.commanded_ang_vel = np.zeros(3)
                    # Clear any active navigation target
                    self.nav_target_pos = None
                    self.nav_target_yaw = None

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
                    # Use logical position to avoid collision drift
                    current_pos = self.robot_logical_pos.copy()
                    current_yaw = self.robot_logical_yaw

                    # Update position based on linear velocity and current orientation
                    dt = self.scene.sim_options.dt
                    # Calculate forward direction from yaw
                    cos_yaw = np.cos(current_yaw)
                    sin_yaw = np.sin(current_yaw)
                    forward_dir = np.array([cos_yaw, sin_yaw, 0.0])

                    # Move in the forward direction
                    new_pos = current_pos + forward_dir * linear_vel * dt

                    # Update orientation based on angular velocity
                    new_yaw = current_yaw + angular_vel * dt
                    new_quat = np.array(
                        [
                            np.cos(new_yaw / 2.0),  # w
                            0.0,  # x
                            0.0,  # y
                            np.sin(new_yaw / 2.0),  # z
                        ]
                    )

                    # Update logical position tracking
                    self.robot_logical_pos = new_pos.copy()
                    self.robot_logical_yaw = new_yaw

                    # Set the new position and orientation on physics mesh
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
                self._update_navigation_movement(dt, sim_time)

            # --- (C) Update moving entities ---
            self._update_entity_poses(sim_time)

            # --- (D) Gather robot pose, velocity (after applying commands)
            # Use logical position for odometry to avoid drift from collision resolution
            pos = self.robot_logical_pos.copy()
            # Convert logical yaw to quaternion [w, x, y, z] format
            quat = np.array(
                [
                    np.cos(self.robot_logical_yaw / 2.0),  # w
                    0.0,  # x
                    0.0,  # y
                    np.sin(self.robot_logical_yaw / 2.0),  # z
                ]
            )

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
                self.chase_camera.set_pose(
                    pos=chase_pos, lookat=robot_pos, up=(0, 0, 1)
                )

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

            # Publish the unified RobotStateMsg to the sensor queue (size-1, latest only)
            # If queue is full, discard old frame and put new one
            try:
                self.shared_queues.sensor_to_agent.put_nowait(state_msg)
            except queue.Full:
                # Discard old frame and put new one
                try:
                    self.shared_queues.sensor_to_agent.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.shared_queues.sensor_to_agent.put_nowait(state_msg)
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
            try:
                self.scene.viewer.stop()
            except Exception:
                pass  # Viewer may already be closed
        print("SimulationNode stopped.")
