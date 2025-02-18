import queue
import numpy as np
import genesis as gs
import cv2  # for potential image saving/processing

from src.simulation.stl_slicing import slice_stl
from src.agent.types import RobotStateMsg, OccupancyGridMsg, VelocityCmd, ResetRobotCmd
from src.simulation.utils import rotate_vector
from src.shared_queues import SharedQueues


class SimulationNode:
    def __init__(self, shared_queues: SharedQueues, enable_vis: bool = True):
        self.shared_queues = shared_queues
        self.enable_vis = enable_vis

        # Initialize core components
        self._init_genesis()
        self._init_scene()
        self._init_environment()
        self._init_robot()
        self._init_camera()
        self._init_map_params()

        self.scene.build()

        self.init_movement()

        print("SimulationNode initialized.")

    def _init_genesis(self):
        """Initialize Genesis backend"""
        gs.init(backend=gs.gpu)

    def _init_scene(self):
        """Initialize the main simulation scene"""
        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(dt=0.1, substeps=10),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(3.5, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=40,
                res=(1280, 720),
            ),
            vis_options=gs.options.VisOptions(
                ambient_light=(0.5, 0.5, 0.5),
            ),
            show_FPS=False,
            show_viewer=self.enable_vis,
        )
        # Add ground plane
        self.scene.add_entity(gs.morphs.Plane())

    def _init_environment(self):
        """Initialize the environment meshes and process occupancy grid"""
        # Add environment meshes
        replica_scene = self.scene.add_entity(
            gs.morphs.Mesh(
                file="data/ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb",
                fixed=True,
                euler=(90, 0, 0),
                pos=(0, 0, -0.1),
                convexify=False,
                collision=False,
            )
        )

        # Export as STL
        file_path = "data/replica_scene.stl"

        self._process_occupancy_grid(file_path)

    def _process_occupancy_grid(self, file_path):
        """
        Process the STL mesh to create an occupancy grid.
        This version also retrieves the mesh bounds so that the map origin can be set
        based on the actual lower-left corner of the geometry.
        """
        slice_height_min = 0
        slice_height_max = slice_height_min + 0.20  # 20cm above the floor
        num_slices = 10
        self.occupancy_grid = None
        self.grid_bounds = None

        for height in np.linspace(slice_height_min, slice_height_max, num_slices):
            grid_slice, bounds = slice_stl(
                stl_path=file_path,
                height=height,
                output_path=f"replica_scene_sliced_{height:.1f}.png",
                pixel_size=0.05,
            )
            if self.occupancy_grid is None:
                self.occupancy_grid = grid_slice
                self.grid_bounds = bounds  # record bounds from the first slice
            else:
                self.occupancy_grid = np.minimum(
                    self.occupancy_grid, grid_slice, dtype=np.uint8
                )

        # Optionally, if needed, flip the grid along the vertical axis so (0,0) is bottom-left
        # self.occupancy_grid = np.flipud(self.occupancy_grid)
        cv2.imwrite("occupancy_grid.png", self.occupancy_grid)

    def _init_robot(self):
        """Initialize robot and its parameters"""
        self.robot = self.scene.add_entity(
            gs.morphs.URDF(file="data/urdf/turtlebot3_burger.urdf", pos=(0, 0, 0))
        )

        # Set up wheel joints
        self.left_idx = self.robot.get_joint("wheel_left_joint").dof_idx_local
        self.right_idx = self.robot.get_joint("wheel_right_joint").dof_idx_local

        # Robot parameters
        self.wheel_radius = 0.033  # meters
        self.wheel_separation = 0.160  # meters

    def _init_camera(self):
        """Initialize robot camera and chase camera"""
        # Original robot camera
        self.robot_camera = self.scene.add_camera(
            res=(640, 480),
            pos=(0, 0, 0),
            lookat=(1, 0, 0),
            fov=60,
        )

        # Add chase camera
        self.chase_camera = self.scene.add_camera(
            res=(640, 480),
            pos=(0, -2.0, 2.0),  # 2m behind, 2m up
            lookat=(0, 0, 0),  # Will be updated to track robot
            fov=60,
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
        self.map_width = self.occupancy_grid.shape[1]  # columns
        self.map_height = self.occupancy_grid.shape[0]  # rows
        self.map_resolution = self.grid_bounds["pixel_size"]
        self.map_origin_x = self.grid_bounds["min_x"]
        self.map_origin_y = self.grid_bounds["min_y"]
        self.map_publish_interval = 10
        self.last_map_publish_step = 0

    def init_movement(self):
        """Initialize robot movement"""
        self.robot.set_dofs_kv([1.0, 1.0], [self.left_idx, self.right_idx])

    def cmd_vel_to_wheel_velocities(self, linear_vel, angular_vel):
        """Convert linear and angular velocity to left and right wheel velocities."""
        # Convert m/s and rad/s to wheel velocities (rad/s)
        left_vel = (
            linear_vel - (angular_vel * self.wheel_separation / 2.0)
        ) / self.wheel_radius
        right_vel = (
            linear_vel + (angular_vel * self.wheel_separation / 2.0)
        ) / self.wheel_radius
        return left_vel, right_vel

    def run(self):
        local_forward = np.array([1.0, 0.0, 0.0])
        step_count = 0

        while not self.shared_queues.exit_event.is_set():
            # --- (B) Gather robot pose, velocity
            pos = self.robot.get_pos().cpu().numpy()
            quat = self.robot.get_quat().cpu().numpy()
            lin_vel = self.robot.get_vel().cpu().numpy()
            ang_vel = self.robot.get_ang().cpu().numpy()

            # --- (C) Render cameras only every 10th frame (i.e. every 1 sec if dt=0.1)
            if step_count % 10 == 0:
                camera_link = self.robot.get_link("camera_link")
                camera_pos = camera_link.get_pos()
                camera_quat = camera_link.get_quat()
                look_dir = rotate_vector(local_forward, camera_quat)
                lookat = camera_pos.cpu().numpy() + look_dir
                self.robot_camera.set_pose(pos=camera_pos.cpu().numpy(), lookat=lookat)

                # Update chase camera to follow robot
                robot_pos = self.robot.get_pos().cpu().numpy()
                robot_quat = self.robot.get_quat().cpu().numpy()
                offset = np.array([-2.0, 0.0, 2.0])  # 2m behind, 2m up
                rotated_offset = rotate_vector(offset, robot_quat)
                chase_pos = robot_pos + rotated_offset
                self.chase_camera.set_pose(pos=chase_pos, lookat=robot_pos)

                # Render both cameras
                # BUG: When we are closing the visualizer and these two instructions are the one running they don't finish and the program gets stuck.
                # Note that I only have it seen on macos, not on linux.
                rgb, depth, seg, normal = self.robot_camera.render(depth=True)
                chase_rgb, _, _, _ = self.chase_camera.render()

                rgb_to_send = rgb
                depth_to_send = depth

                try:
                    camera_views = {"first_person": rgb, "chase": chase_rgb}
                    self.shared_queues.sim_to_web.put_nowait(camera_views)
                except queue.Full:
                    pass
            else:
                rgb_to_send = None
                depth_to_send = None

            # --- (D) Build RobotStateMsg
            state_msg = RobotStateMsg(
                # camera data
                rgb_frame=rgb_to_send,
                depth_frame=depth_to_send,
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

            # Publish the unified RobotStateMsg to the bridge
            try:
                self.shared_queues.sim_to_agent.put_nowait(state_msg)
            except queue.Full:
                pass

            # --- (E) Occasionally publish the map
            if (step_count - self.last_map_publish_step) >= self.map_publish_interval:
                # Build OccupancyGridMsg
                # Suppose self.occupancy_grid is shape=(map_height, map_width), int8
                og_msg = OccupancyGridMsg(
                    width=self.map_width,
                    height=self.map_height,
                    resolution=self.map_resolution,
                    origin_x=self.map_origin_x,
                    origin_y=self.map_origin_y,
                    origin_z=0.0,
                    origin_yaw=0.0,
                    data=self.occupancy_grid,
                    frame_id="map",
                )
                try:
                    self.shared_queues.sim_to_agent.put_nowait(og_msg)
                except queue.Full:
                    pass
                self.last_map_publish_step = step_count

            # --- (F) Handle velocity commands from agent -> sim
            try:
                cmd = self.shared_queues.agent_to_sim.get_nowait()
                if isinstance(cmd, VelocityCmd):
                    linear_vel = cmd.linear_x
                    angular_vel = cmd.angular_z
                    left_vel, right_vel = self.cmd_vel_to_wheel_velocities(
                        linear_vel, angular_vel
                    )
                    self.robot.control_dofs_velocity(
                        [left_vel, right_vel], [self.left_idx, self.right_idx]
                    )
                elif isinstance(cmd, ResetRobotCmd):
                    print("[SimulationNode] Resetting robot pose to origin.")
                    self.robot.set_pos((0, 0, 0))
                    self.robot.set_quat((0, 0, 0, 1))
                    self.robot.control_dofs_velocity(
                        [0.0, 0.0], [self.left_idx, self.right_idx]
                    )
            except queue.Empty:
                pass

            # --- (G) Step the physics
            try:
                self.scene.step()
                step_count += 1
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
