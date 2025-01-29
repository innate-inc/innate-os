from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch.conditions import IfCondition, UnlessCondition
from launch_ros.actions import Node


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")
    localization = LaunchConfiguration("localization")

    parameters = {
        "frame_id": "base_footprint",
        "use_sim_time": use_sim_time,
        "subscribe_depth": True,
        "use_action_for_goal": True,
        "Reg/Force3DoF": "true",
        "Grid/RayTracing": "true",
        "Grid/3D": "false",
        "Grid/RangeMax": "3",
        "Grid/NormalsSegmentation": "false",
        "Grid/MaxGroundHeight": "0.05",
        "Grid/MaxObstacleHeight": "0.4",
        "Optimizer/GravitySigma": "0",
    }

    remappings = [
        ("rgb/image", "/camera/color/image_raw"),
        ("rgb/camera_info", "/camera/camera_info"),
        ("depth/image", "/camera/depth/image_raw"),
    ]

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "use_sim_time",
                default_value="true",
                description="Use simulation (Gazebo or other) clock if true",
            ),
            DeclareLaunchArgument(
                "localization",
                default_value="false",
                description="Launch in localization mode.",
            ),
            # SLAM Mode
            Node(
                condition=UnlessCondition(localization),
                package="rtabmap_slam",
                executable="rtabmap",
                output="screen",
                parameters=[parameters],
                remappings=remappings,
                arguments=["-d"],
            ),  # '-d' => delete previous DB
            # Localization Mode
            Node(
                condition=IfCondition(localization),
                package="rtabmap_slam",
                executable="rtabmap",
                output="screen",
                parameters=[
                    parameters,
                    {
                        "Mem/IncrementalMemory": "False",
                        "Mem/InitWMWithAllNodes": "True",
                    },
                ],
                remappings=remappings,
            ),
            # Depth -> Point Cloud
            Node(
                package="rtabmap_util",
                executable="point_cloud_xyz",
                output="screen",
                parameters=[{"decimation": 2, "max_depth": 3.0, "voxel_size": 0.02}],
                remappings=[
                    ("depth/image", "/camera/depth/image_raw"),
                    ("depth/camera_info", "/camera/camera_info"),
                    ("cloud", "/camera/cloud"),
                ],
            ),
            # Obstacle detection
            Node(
                package="rtabmap_util",
                executable="obstacles_detection",
                output="screen",
                parameters=[parameters],
                remappings=[
                    ("cloud", "/camera/cloud"),
                    ("obstacles", "/camera/obstacles"),
                    ("ground", "/camera/ground"),
                ],
            ),
        ]
    )
