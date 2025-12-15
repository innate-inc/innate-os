from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(
            package='innate_webrtc_streamer',
            executable='webrtc_streamer_node',
            name='webrtc_streamer',
            output='screen',
            parameters=[{
                'live_main_camera_topic': '/mars/main_camera/image/compressed',
                'live_arm_camera_topic': '/mars/arm/image_raw/compressed',
                'replay_main_camera_topic': '/brain/recorder/replay/main_camera/compressed',
                'replay_arm_camera_topic': '/brain/recorder/replay/arm_camera/compressed',
            }]
        )
    ])
