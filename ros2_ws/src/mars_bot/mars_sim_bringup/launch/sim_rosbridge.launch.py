# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Use C++ RWS server instead of Python rosbridge for better performance
    # (~4.5x lower CPU usage)
    rws_node = Node(
        package="rws",
        executable="rws_server",
        name="rosbridge_websocket",  # Keep same node name for compatibility
        output="screen",
        parameters=[
            {
                "port": 9090,
                "rosbridge_compatible": True,
                # Sim runs on /clock (mars_sim_driver publishes it).
                "use_sim_time": True,
            }
        ],
        # Disable Zenoh SHM for rws_server to prevent __pthread_tpp_change_priority
        # crash from priority-inheritance mutex contention between websocketpp threads
        # and Zenoh SHM watchdog threads. rws_server is a JSON bridge — no SHM needed.
        # Without this the server binds port 9090 but hangs every websocket handshake.
        # Mirrors the same workaround in mars_control/launch/app.launch.py.
        additional_env={
            "ZENOH_SESSION_CONFIG_OVERRIDE": (
                "transport/shared_memory/enabled=false"
                ";transport/link/tx/queue/congestion_control/drop/wait_before_drop=900000"
                ";transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=900000"
            ),
        },
    )

    return LaunchDescription([rws_node])
