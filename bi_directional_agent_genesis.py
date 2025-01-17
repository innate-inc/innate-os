import argparse
import cv2
import numpy as np
import threading
import queue
import time

import genesis as gs

############################
# UTILITY FUNCTIONS
############################


def quaternion_to_matrix(q):
    w, x, y, z = q.cpu().numpy()
    ww, xx_, yy, zz = w * w, x * x, y * y, z * z
    R = np.array(
        [
            [ww + xx_ - yy - zz, 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), ww - xx_ + yy - zz, 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), ww - xx_ - yy + zz],
        ]
    )
    return R


def rotate_vector(vec, quat):
    R = quaternion_to_matrix(quat)
    return R.dot(vec)


############################
# SHARED STRUCTURES
############################


class SharedQueues:
    """
    Minimal message broker:
    - sim_to_agent: images (and optionally robot poses)
    - agent_to_sim: control commands
    """

    def __init__(self):
        self.sim_to_agent = queue.Queue(maxsize=1)
        self.agent_to_sim = queue.Queue(maxsize=1)
        self.exit_event = threading.Event()


############################
# SIMULATION NODE
############################


class SimulationNode:
    def __init__(self, shared_queues, enable_vis=True):
        self.shared_queues = shared_queues
        self.enable_vis = enable_vis

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

        plane = self.scene.add_entity(gs.morphs.Plane())
        replica_scene = self.scene.add_entity(
            gs.morphs.Mesh(
                file="ReplicaCAD_baked_lighting/stages_uncompressed/Baked_sc0_staging_00.glb",
                fixed=True,
                euler=(90, 0, 0),
                pos=(0, 0, -0.1),
                convexify=False,
                collision=False,
            )
        )

        self.robot = self.scene.add_entity(
            gs.morphs.URDF(file="urdf/turtlebot3_burger.urdf", pos=(0, 0, 0))
        )

        self.robot_camera = self.scene.add_camera(
            res=(640, 480),
            pos=(0, 0, 0),
            lookat=(1, 0, 0),
            fov=60,
        )

        self.scene.build()

        # Identify joint indices
        self.left_idx = self.robot.get_joint("wheel_left_joint").dof_idx_local
        self.right_idx = self.robot.get_joint("wheel_right_joint").dof_idx_local
        self.robot.set_dofs_kv([1.0, 1.0], [self.left_idx, self.right_idx])

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

            camera_link = self.robot.get_link("camera_link")
            camera_pos = camera_link.get_pos()
            camera_quat = camera_link.get_quat()

            look_dir = rotate_vector(local_forward, camera_quat)
            lookat = camera_pos.cpu().numpy() + look_dir

            self.robot_camera.set_pose(pos=camera_pos.cpu().numpy(), lookat=lookat)

            # Command forward velocity on each wheel
            self.robot.control_dofs_velocity(
                [2.0, 2.0], [self.left_idx, self.right_idx]
            )

            # Check for new velocity commands
            # try:
            #     cmd = self.shared_queues.agent_to_sim.get_nowait()
            #     self.robot.control_dofs_velocity(cmd, [self.left_idx, self.right_idx])
            # except queue.Empty:
            #     pass

            # Render camera
            rgb, depth, seg, normal = self.robot_camera.render(depth=True)

            # Publish observation
            try:
                self.shared_queues.sim_to_agent.put_nowait((rgb, depth))
            except queue.Full:
                pass

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

                # Save the RGB and depth images to files
                # cv2.imwrite(f"rgb_{step_count}.png", rgb)
                # cv2.imwrite(f"depth_{step_count}.png", depth)

            t_prev = t_now

        # Cleanup if needed
        if self.enable_vis:
            self.scene.viewer.stop()
        print("SimulationNode stopped.")


############################
# AGENT NODE
############################


def agent_loop(shared_queues):
    """
    Subscribes to images from the simulation; publishes velocity commands.
    """
    while not shared_queues.exit_event.is_set():
        try:
            rgb, depth = shared_queues.sim_to_agent.get(timeout=0.1)
            # Example: Always go forward at 2.0
            new_command = [2.0, 2.0]
            try:
                shared_queues.agent_to_sim.put_nowait(new_command)
            except queue.Full:
                pass
        except queue.Empty:
            continue

    print("AgentNode stopped.")


############################
# MAIN
############################


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--vis", action="store_true", default=True)
    args = parser.parse_args()

    shared_queues = SharedQueues()

    sim_node = SimulationNode(shared_queues, enable_vis=args.vis)

    # Start the agent in a normal Python thread
    agent_thread = threading.Thread(
        target=agent_loop, args=(shared_queues,), daemon=True
    )
    agent_thread.start()

    # Now, run the simulation in *another* thread (via Genesis's run_in_another_thread).
    gs.tools.run_in_another_thread(fn=sim_node.run, args=())

    # If visualization is requested, start the viewer in the main thread
    # to avoid macOS event-loop constraints.
    if args.vis:
        sim_node.scene.viewer.start()

    try:
        # Keep main thread alive until user interrupts
        while not shared_queues.exit_event.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Keyboard interrupt, shutting down...")
    finally:
        # Signal both threads to stop
        shared_queues.exit_event.set()
        agent_thread.join()
        print("Main finished.")


if __name__ == "__main__":
    main()
