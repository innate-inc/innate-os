"""Virtual MARS: the sim driver + robot_state_publisher fed the same
mars.urdf the real bringup uses (static frames: base_footprint, base_laser,
camera_optical_frame, arm links). AMCL owns map->odom; the driver broadcasts
odom->base_link itself, like the real base driver.

The world (physics + rendering) always runs in the host world server; the
driver node here is a thin client of VIRTUAL_MARS_REMOTE. There is no
in-container world: this container ships no MuJoCo, and software GL was
slow enough to break teleop (~105ms/frame + a starved ROS stack)."""

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import SetEnvironmentVariable
from launch_ros.actions import Node


def generate_launch_description():
    if not os.environ.get("VIRTUAL_MARS_REMOTE", "").strip():
        raise RuntimeError(
            "VIRTUAL_MARS_REMOTE is not set -- the sim world runs on the host, never in-container.\n"
            "Start the stack with `./innate-sim up` (which launches the host world server), or run\n"
            "`mars_sim_driver world_server` yourself and point VIRTUAL_MARS_REMOTE at it (host:port)."
        )

    urdf = Path(get_package_share_directory("mars_sim")) / "urdf" / "mars.urdf"

    return LaunchDescription(
        [
            # -O strips rosidl's per-byte message asserts: with raw camera +
            # depth + points subscribed they burned ~1.5 cores and starved
            # the executor (command callbacks dropped to ~5Hz).
            SetEnvironmentVariable("PYTHONOPTIMIZE", "1"),
            Node(
                package="robot_state_publisher",
                executable="robot_state_publisher",
                name="robot_state_publisher",
                output="screen",
                parameters=[{"robot_description": urdf.read_text()}],
            ),
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
            ),
        ]
    )
