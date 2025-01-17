import argparse
import cv2
import numpy as np

import genesis as gs


def quaternion_to_matrix(q):
    """
    Convert a quaternion q = (w, x, y, z) into a 3x3 rotation matrix.
    """
    w, x, y, z = q.cpu().numpy()
    # Precompute squares
    ww, xx, yy, zz = w * w, x * x, y * y, z * z

    # Rotation matrix, assuming q is normalized
    R = np.array(
        [
            [ww + xx - yy - zz, 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), ww - xx + yy - zz, 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), ww - xx - yy + zz],
        ],
    )
    return R


def rotate_vector(vec, quat):
    """
    Rotate a 3D vector `vec` by a quaternion `quat` = (w, x, y, z).
    """
    R = quaternion_to_matrix(quat)
    return R.dot(vec)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=True)
    args = parser.parse_args()

    ########################## init ##########################
    gs.init(backend=gs.gpu)

    ########################## create a scene ##########################
    scene = gs.Scene(
        sim_options=gs.options.SimOptions(),
        viewer_options=gs.options.ViewerOptions(
            camera_pos=(3.5, 0.0, 2.5),
            camera_lookat=(0.0, 0.0, 0.5),
            camera_fov=40,
            res=(1920, 1080),
        ),
        show_viewer=args.vis,
        rigid_options=gs.options.RigidOptions(
            dt=0.01,
            gravity=(0.0, 0.0, -10.0),
        ),
    )

    ########################## entities ##########################
    plane = scene.add_entity(gs.morphs.Plane())

    replica_scene = scene.add_entity(
        gs.morphs.Mesh(
            file="ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb",
            fixed=True,
            euler=(90, 0, 0),
            pos=(0, 0, -0.1),
            convexify=False,
            collision=False,
        )
    )

    # Load your TurtleBot3 URDF
    r0 = scene.add_entity(
        gs.morphs.URDF(file="urdf/turtlebot3_burger.urdf", pos=(0, 0, 0)),
    )

    # Add robot camera
    robot_camera = scene.add_camera(
        res=(640, 480),
        pos=(0, 0, 0),  # Will be updated each frame
        lookat=(1, 0, 0),
        fov=60,
        # GUI=True,
    )

    ########################## build ##########################
    scene.build()

    # Identify wheel joints
    left_idx = r0.get_joint("wheel_left_joint").dof_idx_local
    right_idx = r0.get_joint("wheel_right_joint").dof_idx_local

    # Set gains for velocity control
    r0.set_dofs_kv([1.0, 1.0], [left_idx, right_idx])

    # Run the main loop in another thread
    gs.tools.run_in_another_thread(
        fn=run_sim, args=(scene, args.vis, r0, left_idx, right_idx, robot_camera)
    )
    if args.vis:
        scene.viewer.start()


def run_sim(scene, enable_vis, robot, left_idx, right_idx, robot_camera):
    from time import time

    t_prev = time()

    # We'll define a "forward" vector for the camera in local coordinates:
    local_forward = np.array([1.0, 0.0, 0.0], dtype=np.float64)

    i = 0
    while True:
        i += 1

        # Get the camera link's world position/orientation
        camera_link = robot.get_link("camera_link")
        camera_pos = camera_link.get_pos()  # (x, y, z)
        camera_quat = camera_link.get_quat()  # (w, x, y, z)

        # Compute a look-direction by rotating local_forward by the camera's quat
        look_dir = rotate_vector(local_forward, camera_quat)

        # Construct the lookat point = camera_pos + look_dir
        lookat = camera_pos.cpu().numpy() + look_dir

        # Update the robot camera's pose
        robot_camera.set_pose(pos=camera_pos.cpu().numpy(), lookat=lookat)

        # Command forward velocity on each wheel
        robot.control_dofs_velocity([2.0, 2.0], [left_idx, right_idx])

        # Get camera renders including depth
        rgb, depth, segmentation, normal = robot_camera.render(depth=True)
        if i % 100 == 0:
            print(f"Depth range: {depth.min():.3f} to {depth.max():.3f}")
            # Output the RGB and depth to files
            # For the depth, we need to normalize it to 0-255
            depth_normalized = (depth - depth.min()) / (depth.max() - depth.min())
            depth_normalized = (depth_normalized * 255).astype(np.uint8)
            cv2.imwrite(f"rgb_{i}.png", rgb)
            cv2.imwrite(f"depth_{i}.png", depth_normalized)

        # Step the simulation
        scene.step()

        # Print FPS
        t_now = time()
        fps = 1.0 / (t_now - t_prev)
        print(f"{fps:.2f} FPS")
        t_prev = t_now

        if i > 100:
            break

    if enable_vis:
        scene.viewer.stop()


if __name__ == "__main__":
    main()
