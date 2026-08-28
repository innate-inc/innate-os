#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Composable node launch file for mars_cam.

All camera nodes run in a single process using ROS 2 composition.
Intra-process communication passes image data via shared_ptr (zero-copy).

Pipeline:
  MainCameraDriver → StereoDepthEstimator (VPI SGM + filters + depth + points)
                   → CameraInfo (published directly by MainCameraDriver)
  ArmCameraDriver
  WebRTCStreamer
  Remote throttle relays (lazy, 2 Hz) for RViz on a different machine

Usage:
    ros2 launch mars_cam camera_composable.launch.py
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer, Node
from launch_ros.descriptions import ComposableNode
from launch_ros.substitutions import FindPackageShare
from mars_bringup.config_loader import settings_params

from mars_cam.remote_throttle_nodes import make_remote_throttle_nodes


def generate_launch_description():
    use_sim_time_arg = DeclareLaunchArgument("use_sim_time", default_value="false", description="Use simulation time")

    camera_config_arg = DeclareLaunchArgument(
        "camera_config",
        default_value=PathJoinSubstitution([FindPackageShare("mars_cam"), "config", "stereo_depth_estimator.yaml"]),
        description="Path to camera pipeline config file",
    )

    start_calibration_manager_arg = DeclareLaunchArgument(
        "start_calibration_manager",
        default_value="true",
        description="Start managed stereo calibration action server node",
    )

    # ── Nodes ─────────────────────────────────────────────────────────────────

    main_camera_node = ComposableNode(
        package="mars_cam",
        plugin="mars_cam::MainCameraDriver",
        name="main_camera_driver",
        parameters=[
            LaunchConfiguration("camera_config"),
            {"use_sim_time": LaunchConfiguration("use_sim_time")},
            *settings_params(),
        ],
        extra_arguments=[{"use_intra_process_comms": True}],
    )

    arm_camera_node = ComposableNode(
        package="mars_cam",
        plugin="mars_cam::ArmCameraDriver",
        name="arm_camera_driver",
        parameters=[
            LaunchConfiguration("camera_config"),
            {"use_sim_time": LaunchConfiguration("use_sim_time")},
            *settings_params(),
        ],
        extra_arguments=[{"use_intra_process_comms": True}],
    )

    webrtc_node = ComposableNode(
        package="mars_cam",
        plugin="mars_cam::WebRTCStreamer",
        name="webrtc_streamer",
        parameters=[
            {
                "use_sim_time": LaunchConfiguration("use_sim_time"),
                "live_main_camera_topic": "/mars/main_camera/left/image_raw",
                "live_arm_camera_topic": "/mars/arm/image_raw",
                "replay_main_camera_topic": "/brain/recorder/replay/main_camera/left/image_raw",
                "replay_arm_camera_topic": "/brain/recorder/replay/arm_camera/image_raw",
                # Mic to teleoperator. Address the Arducam by stable card name (sysdefault:CARD=)
                # rather than a positional index, which can shift across boots/USB enumeration.
                "enable_audio": True,
                "audio_source_element": "alsasrc",
                "audio_capture_device": "sysdefault:CARD=Light",
                "enable_talkback": True,  # operator voice out the robot speaker
                # Cancel the speaker out of the mic (webrtcdsp): the robot stops hearing itself and
                # the half-duplex duck lifts, so an unmuted operator still hears the room. Degrades
                # to plain half-duplex if the plugin is missing.
                "enable_echo_cancel": True,
                # Bound the teleop receiver's de-jitter buffer (ms) via the playout-delay RTP
                # extension. Measured effect on the LAN: once this extension advertises playout-delay
                # support, Chrome (which already pins jitterBufferTarget=0) drops the buffer from
                # ~75 ms to ~2 ms with no added loss. max is the effective lever — it caps how far the
                # buffer may grow during jitter spikes, bounding worst-case latency below the 75 ms
                # default. min is inert while the client pins target=0 (kept at 0). Raise max for
                # smoother playout under jitter, lower it for tighter latency. Retunable via restart.
                "playout_min_delay_ms": 0,
                "playout_max_delay_ms": 40,
                # Local-only STUN Binding responder. Browsers still obfuscate host candidates as mDNS,
                # but when they query stun:<robot-lan-ip>:3478, the srflx candidate they emit is the LAN
                # IP:port observed by the robot, not a public NAT hairpin route.
                "enable_local_stun": True,
                "local_stun_port": 3478,
                # Seconds without any RTCP from the peer before the node tears the pipeline down and
                # releases the camera subscriptions / encoders / mic. webrtcbin never reports a
                # vanished (closed-tab/killed) peer, so this inactivity watchdog is what makes the
                # subscriptions lazy. Must exceed the peer's RTCP interval with margin: a browser's RR
                # cadence for low-bitrate video can stretch to ~5 s, so a 5 s timeout false-positives and
                # tears down a perfectly healthy connection. 15 s gives ~3 missed reports of slack; a
                # genuinely-dead peer is still caught here and by the ICE DISCONNECTED/FAILED path.
                # Runtime-tunable via `ros2 param set`.
                "rtcp_inactivity_timeout_s": 15.0,
            },
            *settings_params(),
        ],
        extra_arguments=[{"use_intra_process_comms": True}],
    )

    depth_estimator_node = ComposableNode(
        package="mars_cam",
        plugin="mars_cam::StereoDepthEstimator",
        name="stereo_depth_estimator",
        parameters=[
            LaunchConfiguration("camera_config"),
            {"use_sim_time": LaunchConfiguration("use_sim_time")},
            *settings_params(),
        ],
        extra_arguments=[{"use_intra_process_comms": True}],
    )

    # ── Container ─────────────────────────────────────────────────────────────

    # ── Remote throttle relays (lazy, intra-process zero-copy input) ────────
    throttle_nodes = make_remote_throttle_nodes()

    # ── Container ─────────────────────────────────────────────────────────────

    camera_container = ComposableNodeContainer(
        name="camera_container",
        namespace="",
        package="rclcpp_components",
        executable="component_container_mt",
        composable_node_descriptions=[
            main_camera_node,
            arm_camera_node,
            webrtc_node,
            depth_estimator_node,
        ]
        + throttle_nodes,
        output="screen",
        emulate_tty=True,
        # Mute the container's per-node "Found class / Instantiate class / Load
        # Library" chatter (one logger: camera_container). The composable nodes
        # keep their own loggers, and launch still prints one "Loaded node …"
        # line per component.
        arguments=["--ros-args", "--log-level", "camera_container:=WARN"],
    )

    stereo_calibration_manager = Node(
        package="mars_cam",
        executable="stereo_calibrator",
        name="stereo_calibration_manager",
        output="screen",
        parameters=[
            {
                "use_sim_time": LaunchConfiguration("use_sim_time"),
                "interactive": False,
                "auto_start": False,
            }
        ],
        condition=IfCondition(LaunchConfiguration("start_calibration_manager")),
    )

    return LaunchDescription(
        [
            use_sim_time_arg,
            camera_config_arg,
            start_calibration_manager_arg,
            camera_container,
            stereo_calibration_manager,
        ]
    )
