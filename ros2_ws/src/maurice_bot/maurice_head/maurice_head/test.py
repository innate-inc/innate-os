#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32
from std_srvs.srv import SetBool
import time

class ServoTest(Node):
    def __init__(self):
        super().__init__('servo_test')
        self.publisher = self.create_publisher(Int32, 'servo_position', 10)
        self.enable_client = self.create_client(SetBool, 'enable_servo')
        
        # Wait for service to be available
        while not self.enable_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info('Waiting for enable_servo service...')
        
        # Enable servo
        self.get_logger().info('Enabling servo...')
        request = SetBool.Request()
        request.data = True
        self.enable_client.call_async(request)
        
        # Wait 1 second
        time.sleep(1.0)
        
        # Move to zero
        self.get_logger().info('Moving to zero...')
        msg = Int32()
        msg.data = 0
        self.publisher.publish(msg)
        
        # Wait 1 second
        time.sleep(1.0)
        
        # Create timer for 30Hz movement
        self.timer = self.create_timer(1.0/30.0, self.timer_callback)
        self.start_time = time.time()
        self.duration = 2.0  # 2 seconds
        self.start_pos = 0
        self.end_pos = 350

    def timer_callback(self):
        current_time = time.time() - self.start_time
        
        if current_time >= self.duration:
            self.timer.cancel()
            self.get_logger().info('Movement complete')
            return
        
        # Calculate position based on elapsed time
        progress = current_time / self.duration
        current_pos = int(self.start_pos + (self.end_pos - self.start_pos) * progress)
        
        msg = Int32()
        msg.data = current_pos
        self.publisher.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = ServoTest()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
