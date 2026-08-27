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

import os

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
                # Playout-delay RTP extension = the CEILING the receiver may buffer, not a target.
                # The webapp adapts receiver.jitterBufferTarget itself: 0 on a clean LAN (measured
                # ~2 ms buffer once this extension is advertised), ~1.2x RTT on a lossy remote path
                # so NACK resends land before playout. max must leave headroom for that adaptation;
                # min stays 0 so the LAN case keeps its lowest latency.
                "playout_min_delay_ms": 0,
                "playout_max_delay_ms": 500,
                # Video loss repair (webrtcbin negotiates none by default — one lost packet then
                # freezes the stream until a PLI keyframe round-trip). ULPFEC repairs losses with
                # no round trip; NACK resends land within the receiver's adaptive jitter buffer.
                "video_nack": True,
                "video_fec_percentage": 25,
                # Per-camera vp8enc target. The sum (+~25% FEC, + retransmits under loss) must fit
                # the site uplink with margin — 2000 each measurably congested a residential link
                # once two cameras streamed, and the resulting keyframe storms compounded the loss.
                "main_bitrate_kbps": 1500,
                "arm_bitrate_kbps": 800,
                # Must match the drivers' real capture rates (stereo_depth_estimator.yaml): the encoder's
                # caps, per-frame rate-control budget, and the 4 s keyframe backstop all key off it.
                "main_fps": 15,
                "arm_fps": 30,
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

    # Opt-in newer GStreamer: a robot with a parallel build staged at /opt/gst
    # (scripts/update/build_gst_opt.sh) runs the camera container against it — 1.20's
    # webrtcbin negotiates ULPFEC but never sends it; >=1.22 does. GStreamer's ABI
    # guarantee lets the 1.20-built binaries run unmodified; NVIDIA's hardware plugins
    # stay on the system path (verified loading under the 1.24 core). Robots without
    # /opt/gst are untouched; INNATE_GST_OPT=0 forces the system stack (per-robot rollback
    # that survives the apt package being reinstalled by the next update).
    gst_opt = "/opt/gst/lib/aarch64-linux-gnu"
    system_ld = os.environ.get("LD_LIBRARY_PATH", "")
    gst_env = (
        {
            # Never leave an empty element (ld.so reads "" as the cwd) when the parent
            # environment has no LD_LIBRARY_PATH.
            "LD_LIBRARY_PATH": gst_opt + (":" + system_ld if system_ld else ""),
            "GST_PLUGIN_PATH": gst_opt + "/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0",
            "GST_REGISTRY": "/tmp/gst-opt-camera.registry",
        }
        if os.path.isdir(gst_opt) and os.environ.get("INNATE_GST_OPT") != "0"
        else {}
    )

    camera_container = ComposableNodeContainer(
        name="camera_container",
        namespace="",
        package="rclcpp_components",
        executable="component_container_mt",
        additional_env=gst_env,
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
