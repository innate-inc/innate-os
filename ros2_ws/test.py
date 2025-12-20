#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
import numpy as np
import tf2_ros
from tf_transformations import euler_from_quaternion

class HeadingRecorder(Node):
    def __init__(self):
        super().__init__('heading_recorder')
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
        self.headings = []

    def run(self):
        try:
            while rclpy.ok():
                # process incoming TF messages
                rclpy.spin_once(self, timeout_sec=0)
                try:
                    # lookup from odom → base_link
                    now = rclpy.time.Time()
                    t = self.tf_buffer.lookup_transform('odom', 'base_link', now)
                    q = t.transform.rotation
                    # convert to yaw
                    _, _, yaw = euler_from_quaternion([q.x, q.y, q.z, q.w])
                    self.headings.append(yaw)
                except (tf2_ros.LookupException, tf2_ros.ExtrapolationException):
                    # no valid transform yet
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            # save all collected headings
            np.save('headings.npy', np.array(self.headings))
            self.get_logger().info(f"Saved {len(self.headings)} samples to headings.npy")

def main(args=None):
    rclpy.init(args=args)
    node = HeadingRecorder()
    node.run()
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    print('hello')
    main()

