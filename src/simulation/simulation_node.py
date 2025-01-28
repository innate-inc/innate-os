import queue
import time
import numpy as np
import genesis as gs
import cv2  # for potential image saving/processing

from src.simulation.stl_slicing import slice_stl
from src.agent.types import RobotStateMsg, OccupancyGridMsg, VelocityCmd
from src.simulation.utils import rotate_vector
from src.shared_queues import SharedQueues


class SimulationNode:
    def __init__(self, shared_queues: SharedQueues, enable_vis: bool = True):
        self.shared_queues = shared_queues
        self.enable_vis = enable_vis

        # Initialize Genesis, build scene, etc.
        gs.init(backend=gs.gpu)

        self.scene = gs.Scene(
            sim_options=gs.options.SimOptions(),
            viewer_options=gs.options.ViewerOptions(
                camera_pos=(3.5, 0.0, 2.5),
                camera_lookat=(0.0, 0.0, 0.5),
                camera_fov=40,
                res=(1280, 720),
            ),
            show_viewer=self.enable_vis,
            rigid_options=gs.options.RigidOptions(
                dt=0.01,
                gravity=(0.0, 0.0, -10.0),
            ),
        )

        # Add ground plane
        plane = self.scene.add_entity(gs.morphs.Plane())

        # Add environment
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

        stl_replica_scene = self.scene.add_entity(
            gs.morphs.Mesh(file="data/replica_scene.stl")
        )

        # Get the mesh data to determine room dimensions
        mesh = gs.Mesh.from_morph_surface(stl_replica_scene.morph)[0]._mesh
        vertices = np.array(mesh.vertices)

        # Get the height bounds (z-axis after 90-degree rotation)
        min_height = np.min(vertices[:, 2])
        max_height = np.max(vertices[:, 2])
        print(f"Room height bounds: {min_height:.2f}m to {max_height:.2f}m")

        # Define the slice range (0-20cm from floor)
        slice_height_min = min_height  # floor level
        slice_height_max = min_height + 0.20  # 20cm above floor

        # Calculate percentage equivalents for the slice_stl function
        total_height = max_height - min_height
        min_percent = 0  # floor level
        max_percent = ((slice_height_max - min_height) / total_height) * 100

        # Export slices within the 0-20cm range and use the min value as the occupancy grid
        occupancy_grid = None
        num_slices = 10  # adjust this for more or fewer slices
        for percent in np.linspace(min_percent, max_percent, num_slices):
            array_at_height = slice_stl(
                stl_path="data/replica_scene.stl",
                height_percent=percent,
                output_path=f"replica_scene_sliced_{percent:.1f}.png",
                pixel_size=0.05,
            )
            if occupancy_grid is None:
                occupancy_grid = array_at_height
            else:
                occupancy_grid = np.minimum(
                    occupancy_grid, array_at_height, dtype=np.uint8
                )

        # Export the occupancy grid as a PNG
        cv2.imwrite("occupancy_grid.png", occupancy_grid)

        # Add robot
        self.robot = self.scene.add_entity(
            gs.morphs.URDF(file="data/urdf/turtlebot3_burger.urdf", pos=(0, 0, 0))
        )

        # Add robot camera
        self.robot_camera = self.scene.add_camera(
            res=(640, 480),
            pos=(0, 0, 0),
            lookat=(1, 0, 0),
            fov=60,
        )

        # Example intrinsics calculation (approx) for a 640×480 camera + 60° HFOV
        # fx = fy = width/(2*tan(HFOV/2)) => 640/(2*tan(30°)) => ~554.256
        # principal point (cx, cy) = (320, 240)
        # no distortion
        self.fx = 554.256
        self.fy = 554.256
        self.cx = 320.0
        self.cy = 240.0
        self.width = 640
        self.height = 480

        self.map_width = 400
        self.map_height = 400
        self.map_resolution = 0.05

        # Also store last time we published map or last step_count
        self.map_publish_interval = 5  # steps
        self.last_map_publish_step = 0

        self.scene.build()

        # Identify joint indices for wheels
        self.left_idx = self.robot.get_joint("wheel_left_joint").dof_idx_local
        self.right_idx = self.robot.get_joint("wheel_right_joint").dof_idx_local
        self.robot.set_dofs_kv([1.0, 1.0], [self.left_idx, self.right_idx])

        # Add robot parameters for differential drive
        self.wheel_radius = 0.033  # meters (from URDF)
        self.wheel_separation = 0.160  # meters (from URDF)

        print("SimulationNode initialized.")

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
        step_count = 0
        while not self.shared_queues.exit_event.is_set():
            step_count += 1

            # --- (A) Step the physics
            try:
                self.scene.step()
            except Exception as e:
                print("Error stepping scene:", e)
                self.shared_queues.exit_event.set()
                break

            # --- (B) Gather robot pose, velocity
            pos = self.robot.get_pos().cpu().numpy()  # [px, py, pz]
            quat = self.robot.get_quat().cpu().numpy()  # [ox, oy, oz, ow]
            lin_vel = self.robot.get_vel().cpu().numpy()  # [vx, vy, vz]
            ang_vel = self.robot.get_angular_vel().cpu().numpy()  # [wx, wy, wz]

            # --- (C) Render camera
            rgb, depth, seg, normal = self.robot_camera.render(depth=True)

            # --- (D) Build a single RobotStateMsg that includes
            #          images + camera info + odometry
            state_msg = RobotStateMsg(
                # camera data
                rgb_frame=rgb,
                depth_frame=depth,
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
                ox=quat[0],
                oy=quat[1],
                oz=quat[2],
                ow=quat[3],
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
                    origin_x=0.0,
                    origin_y=0.0,
                    origin_z=0.0,
                    origin_yaw=0.0,
                    data=self.occupancy_grid,  # The np.ndarray
                    frame_id="map",
                )
                try:
                    self.shared_queues.sim_to_agent.put_nowait(og_msg)
                except queue.Full:
                    pass
                self.last_map_publish_step = step_count

            # --- (F) Optionally handle velocity commands from agent -> sim
            try:
                cmd = self.shared_queues.agent_to_sim.get_nowait()
                if isinstance(cmd, VelocityCmd):
                    # convert to wheel velocities or whatever
                    # self.robot.control_dofs_velocity(...)
                    pass
            except queue.Empty:
                pass

            # small sleep or continue
            # time.sleep(0.01)

        # Cleanup
        if self.enable_vis:
            self.scene.viewer.stop()
        print("SimulationNode stopped.")
