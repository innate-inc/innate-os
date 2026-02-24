from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(
            package='maurice_cam',
            executable='webrtc_streamer_node',
            name='webrtc_streamer',
            output='screen',
            parameters=[{
                'use_compressed_images': True,  # Use compressed images for sim/rosbridge
                'live_main_camera_topic': '/mars/main_camera/left/image_raw',
                'live_arm_camera_topic': '/mars/arm/image_raw',
                'replay_main_camera_topic': '/brain/recorder/replay/main_camera/left/image_raw',
                'replay_arm_camera_topic': '/brain/recorder/replay/arm_camera/image_raw',
            }]
        )
    ])
