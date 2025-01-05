import argparse

import genesis as gs


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
        ),
        show_viewer=args.vis,
        rigid_options=gs.options.RigidOptions(
            dt=0.01,
            gravity=(0.0, 0.0, -10.0),
        ),
    )

    ########################## entities ##########################
    plane = scene.add_entity(gs.morphs.Plane())

    # Load your TurtleBot3 URDF
    r0 = scene.add_entity(
        gs.morphs.URDF(file="urdf/turtlebot3_burger.urdf"),
    )

    ########################## build ##########################
    scene.build()

    # ------------------------------------------------------------
    # NEW CODE: Identify the wheel joints and set gains
    # ------------------------------------------------------------
    left_idx = r0.get_joint("wheel_left_joint").dof_idx_local
    right_idx = r0.get_joint("wheel_right_joint").dof_idx_local

    # For velocity control, we usually set 'Kv' (velocity gain)
    # (You can also set Kp if you prefer or if your wheels are in a PD control mode)
    r0.set_dofs_kv([1.0, 1.0], [left_idx, right_idx])
    # r0.set_dofs_kp([0.0, 0.0], [left_idx, right_idx])  # Typically 0 if purely velocity-based
    # ------------------------------------------------------------

    # We'll pass the robot and joint indices into run_sim so we can command them there
    gs.tools.run_in_another_thread(
        fn=run_sim, args=(scene, args.vis, r0, left_idx, right_idx)
    )
    if args.vis:
        scene.viewer.start()


def run_sim(scene, enable_vis, robot, left_idx, right_idx):
    from time import time

    t_prev = time()
    i = 0
    while True:
        i += 1

        # ------------------------------------------------------------
        # NEW CODE: Command some constant forward velocity on each wheel
        # ------------------------------------------------------------
        robot.control_dofs_velocity([2.0, 2.0], [left_idx, right_idx])
        # The wheels will rotate with velocity ~2 rad/s each step, moving the robot forward.
        # Adjust up/down to see faster/slower movement.
        # ------------------------------------------------------------

        # Step the simulation
        scene.step()

        t_now = time()
        fps = 1.0 / (t_now - t_prev)
        print(f"{fps:.2f} FPS")
        t_prev = t_now

    if enable_vis:
        scene.viewer.stop()


if __name__ == "__main__":
    main()
