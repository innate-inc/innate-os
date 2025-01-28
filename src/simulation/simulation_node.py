import queue
import time
import numpy as np
import genesis as gs
import cv2  # for potential image saving/processing

from src.simulation.stl_slicing import slice_stl
from src.agent.types import ImageMsg, CameraInfoMsg
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

        # Export slices within the 0-20cm range
        num_slices = 10  # adjust this for more or fewer slices
        for percent in np.linspace(min_percent, max_percent, num_slices):
            slice_stl(
                stl_path="data/replica_scene.stl",
                height_percent=percent,
                output_path=f"replica_scene_sliced_{percent:.1f}.png",
            )

        exit()

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

        # We'll create a single camera info message for color. Depth might share same intrinsics if aligned.
        self.color_camera_info = CameraInfoMsg(
            width=self.width,
            height=self.height,
            fx=self.fx,
            fy=self.fy,
            cx=self.cx,
            cy=self.cy,
            frame_id="camera_color_frame",  # or "camera_link"
            distortion_model="plumb_bob",
            D=[0.0, 0.0, 0.0, 0.0, 0.0],
        )

        self.scene.build()

        # Identify joint indices for wheels
        self.left_idx = self.robot.get_joint("wheel_left_joint").dof_idx_local
        self.right_idx = self.robot.get_joint("wheel_right_joint").dof_idx_local
        self.robot.set_dofs_kv([1.0, 1.0], [self.left_idx, self.right_idx])

        # Add robot parameters for differential drive
        self.wheel_radius = 0.033  # meters (from URDF)
        self.wheel_separation = 0.160  # meters (from URDF)

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
        """
        Main simulation loop.
        Publishes camera data to agent, reads velocity commands from agent.
        """
        local_forward = np.array([1.0, 0.0, 0.0])
        t_prev = time.time()
        step_count = 0

        print("SimulationNode started.")

        while not self.shared_queues.exit_event.is_set():
            step_count += 1

            # Update the camera pose to follow the robot's camera link
            camera_link = self.robot.get_link("camera_link")
            camera_pos = camera_link.get_pos()
            camera_quat = camera_link.get_quat()

            look_dir = rotate_vector(local_forward, camera_quat)
            lookat = camera_pos.cpu().numpy() + look_dir
            self.robot_camera.set_pose(pos=camera_pos.cpu().numpy(), lookat=lookat)

            # Option A: Hard-coded velocity commands (always forward):
            self.robot.control_dofs_velocity(
                [2.0, 2.0], [self.left_idx, self.right_idx]
            )

            # Option B: Read velocity commands from the agent
            try:
                cmd = self.shared_queues.agent_to_sim.get_nowait()
                # Assuming cmd is (linear_vel, angular_vel)
                linear_vel, angular_vel = cmd
                left_vel, right_vel = self.cmd_vel_to_wheel_velocities(
                    linear_vel, angular_vel
                )
                print(
                    f"Received cmd_vel: linear={linear_vel:.2f} m/s, angular={angular_vel:.2f} rad/s"
                )
                print(
                    f"Wheel velocities: left={left_vel:.2f} rad/s, right={right_vel:.2f} rad/s"
                )
                self.robot.control_dofs_velocity(
                    [left_vel, right_vel], [self.left_idx, self.right_idx]
                )
            except queue.Empty:
                pass

            # Render camera
            rgb, depth, seg, normal = self.robot_camera.render(depth=True)

            # Publish observation
            try:
                self.shared_queues.sim_to_agent.put_nowait(ImageMsg(rgb, depth))
            except queue.Full:
                pass

            ### NEW ### Also publish camera info
            try:
                self.shared_queues.sim_to_agent.put_nowait(self.color_camera_info)
            except queue.Full:
                pass

            # For the web server:
            try:
                # Keep only the latest frame in sim_to_web, so empty it first if needed
                if not self.shared_queues.sim_to_web.empty():
                    _ = self.shared_queues.sim_to_web.get_nowait()
                self.shared_queues.sim_to_web.put_nowait(ImageMsg(rgb, None))
            except queue.Full:
                pass

            # Step the physics
            try:
                self.scene.step()
            except Exception as e:
                if "Viewer closed" in str(e):
                    print("Viewer closed, stopping simulation.")
                    self.shared_queues.exit_event.set()
                    break
                else:
                    print(f"Error in SimulationNode: {e}")
                    self.shared_queues.exit_event.set()
                    break

            # Print FPS occasionally
            t_now = time.time()
            if step_count % 20 == 0:
                fps = 1.0 / (t_now - t_prev)
                print(f"{fps:.2f} FPS")

                # (Optional) Save the RGB and depth images to files
                # cv2.imwrite(f"rgb_{step_count}.png", rgb)
                # cv2.imwrite(f"depth_{step_count}.png", depth)

            t_prev = t_now

        # Cleanup if needed
        if self.enable_vis:
            self.scene.viewer.stop()
        print("SimulationNode stopped.")
