# odom_tf_broadcaster.py
import rclpy
from rclpy.node import Node
from tf2_ros import TransformBroadcaster
from nav_msgs.msg import Odometry
from geometry_msgs.msg import TransformStamped


class OdomTfBroadcaster(Node):
    def __init__(self):
        super().__init__("odom_tf_broadcaster")
        self.br = TransformBroadcaster(self)
        self.subscription = self.create_subscription(
            Odometry, "/odom", self.odom_callback, 10  # your odometry topic
        )
        self.get_logger().info("Odom TF Broadcaster initialized")

    def odom_callback(self, odom_msg: Odometry):
        # Build a TransformStamped
        t = TransformStamped()
        t.header.stamp = odom_msg.header.stamp
        t.header.frame_id = odom_msg.header.frame_id  # "odom"
        t.child_frame_id = odom_msg.child_frame_id  # "base_footprint"

        # position
        t.transform.translation.x = odom_msg.pose.pose.position.x
        t.transform.translation.y = odom_msg.pose.pose.position.y
        t.transform.translation.z = odom_msg.pose.pose.position.z

        # orientation
        t.transform.rotation = odom_msg.pose.pose.orientation

        # Broadcast
        self.br.sendTransform(t)


def main(args=None):
    rclpy.init(args=args)
    node = OdomTfBroadcaster()
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == "__main__":
    main()
