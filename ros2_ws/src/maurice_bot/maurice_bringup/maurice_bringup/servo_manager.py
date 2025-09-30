#!/usr/bin/env python3
"""
servo_manager.py

ROS2 node that scans for Dynamixel servos and sets parameters for arm and head devices.
"""

import rclpy
from rclpy.node import Node
import glob
from dynamixel_sdk import PortHandler, PacketHandler, COMM_SUCCESS

# --- CONFIG ---
PROTOCOL_VERSION  = 2.0
BAUDRATE          = 115_200
ADDR_MODEL_NUMBER = 0         # control table addr for Model Number (2 bytes)
ID_RANGE          = range(1, 11)

# human-friendly names for a couple of common models
MODEL_MAP = {
    1060: "XL430",
    1200: "XL330",
    1080: "XC430",        # <— add this line
}

class ServoManager(Node):
    def __init__(self):
        super().__init__('servo_manager')
        self.get_logger().info('Servo Manager node starting...')
        
        # Start the servo scan
        self.scan_servos()
    
    def detect_servos(self, dev_path):
        """Open `dev_path`, read model number at addr 0 for IDs 1–10."""
        port    = PortHandler(dev_path)
        handler = PacketHandler(PROTOCOL_VERSION)
        servos  = []

        if not port.openPort():
            self.get_logger().error(f"Cannot open {dev_path}")
            return servos
        port.setBaudRate(BAUDRATE)

        for sid in ID_RANGE:
            # read2ByteTxRx returns (value, comm_result, error)
            model_num, com_res, err = handler.read2ByteTxRx(
                port, sid, ADDR_MODEL_NUMBER
            )
            if com_res == COMM_SUCCESS:
                servos.append((sid, model_num))
            # else: no servo at that ID (or comm timed out)

        port.closePort()
        return servos
    
    def verify_servo_configuration(self, port_name, servos, expected_count):
        """Verify servo configuration and log details."""
        self.get_logger().info(f"Port: {port_name} → {len(servos)} servo(s) detected")
        
        if len(servos) != expected_count:
            self.get_logger().warn(f"Expected {expected_count} servos, found {len(servos)}")
        
        for sid, model in servos:
            name = MODEL_MAP.get(model, "UNKNOWN")
            self.get_logger().info(f"  • ID={sid:2d}  ModelNumber={model}  ({name})")
        
        return len(servos) == expected_count
    
    def scan_servos(self):
        """Scan for Dynamixel chains and set ROS parameters."""
        self.get_logger().info("Scanning for Dynamixel chains (torque ON, powered servos)...")
        
        arm_device = None
        head_device = None
        
        for dev in ['/dev/ttyTHS1']:
            found = self.detect_servos(dev)
            
            if len(found) == 6:
                # Verify this is the arm device
                if self.verify_servo_configuration(dev, found, 6):
                    arm_device = dev
                    self.get_logger().info(f"Arm device identified: {dev}")
            elif len(found) == 1:
                # Verify this is the head device
                if self.verify_servo_configuration(dev, found, 1):
                    head_device = dev
                    self.get_logger().info(f"Head device identified: {dev}")
            elif len(found) > 0:
                # Log any unexpected servo counts
                self.verify_servo_configuration(dev, found, len(found))
                self.get_logger().warn(f"Unexpected servo count on {dev}")
        
        # Set parameters for any devices found
        self.get_logger().info("Setting ROS parameters...")
        
        # Determine if we're ready (both devices found)
        ready = arm_device is not None and head_device is not None
        
        # Always declare and set ready parameter
        self.declare_parameter('ready', False)
        params_to_set = [
            rclpy.parameter.Parameter('ready', rclpy.Parameter.Type.BOOL, ready)
        ]
        
        # Only declare and set device parameters if devices are found
        if arm_device:
            self.declare_parameter('arm_device', '')
            params_to_set.append(
                rclpy.parameter.Parameter('arm_device', rclpy.Parameter.Type.STRING, arm_device)
            )
            self.get_logger().info(f"Arm device parameter set: {arm_device}")
        else:
            self.get_logger().warn("Arm device (6 servos) not found")
        
        if head_device:
            self.declare_parameter('head_device', '')
            params_to_set.append(
                rclpy.parameter.Parameter('head_device', rclpy.Parameter.Type.STRING, head_device)
            )
            self.get_logger().info(f"Head device parameter set: {head_device}")
        else:
            self.get_logger().warn("Head device (1 servo) not found")
        
        self.set_parameters(params_to_set)
        
        if ready:
            self.get_logger().info(f"All devices found - ready: True")
        else:
            self.get_logger().warn("Not all devices found - ready: False")

def main(args=None):
    rclpy.init(args=args)
    node = ServoManager()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
