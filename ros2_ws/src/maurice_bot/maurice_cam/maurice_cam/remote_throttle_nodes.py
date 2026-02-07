"""
Lazy throttle ComposableNode descriptions for remote RViz viewing.

Each throttle republishes a main-camera topic at 2 Hz under
/mars/main_camera/remote/... and only subscribes when its output
has at least one subscriber (lazy=True), preserving the depth
estimator's lazy-publish behaviour.

Import and append to any ComposableNodeContainer:

    from maurice_cam.remote_throttle_nodes import make_remote_throttle_nodes
    throttle_nodes = make_remote_throttle_nodes(rate=2.0)
"""

from launch_ros.descriptions import ComposableNode


# (input_topic, output_topic, node_name)
_TOPICS = [
    ('/mars/main_camera/left/image_raw',
     '/mars/main_camera/remote/left/image_raw',
     'throttle_left_raw'),

    ('/mars/main_camera/right/image_raw',
     '/mars/main_camera/remote/right/image_raw',
     'throttle_right_raw'),

    ('/mars/main_camera/left/image_rect',
     '/mars/main_camera/remote/left/image_rect',
     'throttle_left_rect'),

    ('/mars/main_camera/right/image_rect',
     '/mars/main_camera/remote/right/image_rect',
     'throttle_right_rect'),

    ('/mars/main_camera/depth/image_rect_raw',
     '/mars/main_camera/remote/depth/image_rect_raw',
     'throttle_depth'),

    ('/mars/main_camera/disparity',
     '/mars/main_camera/remote/disparity',
     'throttle_disparity'),

    ('/mars/main_camera/disparity_unfiltered',
     '/mars/main_camera/remote/disparity_unfiltered',
     'throttle_disparity_unfiltered'),

    ('/mars/main_camera/points',
     '/mars/main_camera/remote/points',
     'throttle_pointcloud'),
]


def make_remote_throttle_nodes(rate: float = 2.0) -> list:
    """Return a list of lazy ThrottleNode ComposableNode descriptions."""
    return [
        ComposableNode(
            package='topic_tools',
            plugin='topic_tools::ThrottleNode',
            name=name,
            parameters=[{
                'input_topic':  input_topic,
                'output_topic': output_topic,
                'throttle_type': 'messages',
                'msgs_per_sec': rate,
                'lazy': True,
                'use_wall_clock': True,
            }],
            extra_arguments=[{'use_intra_process_comms': True}],
        )
        for input_topic, output_topic, name in _TOPICS
    ]
