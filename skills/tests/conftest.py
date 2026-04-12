"""Mock ROS dependencies for skill unit tests."""

import sys
from unittest.mock import MagicMock

for mod in [
    "launch",
    "launch.actions",
    "rclpy",
    "rclpy.node",
    "rclpy.action",
    "action_msgs",
    "action_msgs.msg",
    "geometry_msgs",
    "geometry_msgs.msg",
    "nav2_simple_commander",
    "nav2_simple_commander.robot_navigator",
    "sensor_msgs",
    "sensor_msgs.msg",
    "std_msgs",
    "std_msgs.msg",
    "std_srvs",
    "std_srvs.srv",
    "maurice_msgs",
    "maurice_msgs.srv",
    "brain_messages",
    "brain_messages.action",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
