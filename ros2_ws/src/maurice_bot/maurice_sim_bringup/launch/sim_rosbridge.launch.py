from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import FrontendLaunchDescriptionSource
import os
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Path to the XML file from rosbridge_server
    rosbridge_xml = os.path.join(
        get_package_share_directory("rosbridge_server"),
        "launch",
        "rosbridge_websocket_launch.xml",
    )

    return LaunchDescription(
        [IncludeLaunchDescription(FrontendLaunchDescriptionSource(rosbridge_xml))]
    )
