"""Virtual MARS: the MuJoCo sim driver + robot_state_publisher fed the same
mars.urdf the real bringup uses (static frames: base_footprint, base_laser,
camera_optical_frame, arm links). AMCL owns map->odom; the driver broadcasts
odom->base_link itself, like the real base driver.

The world (physics + rendering) always runs in the world server; the driver
node is a thin client. When VIRTUAL_MARS_REMOTE is set (the launcher's host
world server -- native GL), only the client side starts here. When unset,
a server is started next to the node in-container, at --render-scale 2
because software GL pays per pixel."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import ExecuteProcess, SetEnvironmentVariable
from launch_ros.actions import Node


def generate_launch_description():
    urdf = Path(get_package_share_directory("mars_sim")) / "urdf" / "mars.urdf"

    world_actions = []
    if not os.environ.get("VIRTUAL_MARS_REMOTE", "").strip():
        world_actions.append(
            ExecuteProcess(
                cmd=["ros2", "run", "mars_sim_driver", "world_server", "--render-scale", "2"],
                output="screen",
            )
        )

    return LaunchDescription(
        [
            # Software GL for the in-container world server (no GPU in Docker).
            SetEnvironmentVariable("MUJOCO_GL", os.environ.get("MUJOCO_GL", "osmesa")),
            # -O strips rosidl's per-byte message asserts: with raw camera +
            # depth + points subscribed they burned ~1.5 cores and starved
            # the executor (command callbacks dropped to ~5Hz).
            SetEnvironmentVariable("PYTHONOPTIMIZE", "1"),
            *world_actions,
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                output="screen",
                parameters=[{"robot_description": urdf.read_text(), "use_sim_time": True}],
            ),
            # The driver is the /clock source and deliberately stays on wall
            # time: its timers must keep ticking while the world is paused to
            # publish the frozen clock and accept the unpause.
            Node(
                package="mars_sim_driver",
                executable="sim_driver",
                name="virtual_mars",
                output="screen",
            ),
            # Stand-in for the CUDA-only grid_localizer: satisfies the mode
            # manager's lifecycle + `localize` Trigger, seeding AMCL with the
            # ground-truth pose.
            Node(
                package="mars_sim_driver",
                executable="grid_localizer_sim",
                output="screen",
                parameters=[{"use_sim_time": True}],
            ),
        ]
    )
