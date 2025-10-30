#!/usr/bin/env python3
"""
UDP Leader Receiver Node

Receives leader arm positions via UDP and publishes them to /leader_positions topic.

Network Protocol:
- Port: 9999 (default, configurable)
- Packet Format (38 bytes, little-endian):
  - Bytes 0-1:   Magic header (0xAA55)
  - Bytes 2-5:   Sequence number (uint32)
  - Bytes 6-13:  Timestamp (double, ms since epoch)
  - Bytes 14-17: Servo 1 position (int32)
  - Bytes 18-21: Servo 2 position (int32)
  - Bytes 22-25: Servo 3 position (int32)
  - Bytes 26-29: Servo 4 position (int32)
  - Bytes 30-33: Servo 5 position (int32)
  - Bytes 34-37: Servo 6 position (int32)
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32MultiArray
from std_srvs.srv import SetBool
import socket
import struct
import threading
import time


class UdpLeaderReceiver(Node):
    # Packet format constants
    MAGIC_HEADER = 0xAA55
    PACKET_SIZE = 38
    HEADER_FORMAT = '<H'  # Little-endian uint16
    SEQUENCE_FORMAT = '<I'  # Little-endian uint32
    TIMESTAMP_FORMAT = '<d'  # Little-endian double
    SERVO_FORMAT = '<i'  # Little-endian int32
    FULL_PACKET_FORMAT = '<HId6i'  # Header + sequence + timestamp + 6 servos
    
    def __init__(self):
        super().__init__('udp_leader_receiver')
        
        # Declare parameters
        self.declare_parameter('port', 9999)
        self.declare_parameter('buffer_size', 1024)
        self.declare_parameter('auto_start', True)
        self.declare_parameter('log_rate', 10.0)  # Log every N seconds
        
        # Get parameters
        self.port = self.get_parameter('port').get_parameter_value().integer_value
        self.buffer_size = self.get_parameter('buffer_size').get_parameter_value().integer_value
        self.auto_start = self.get_parameter('auto_start').get_parameter_value().bool_value
        self.log_rate = self.get_parameter('log_rate').get_parameter_value().double_value
        
        # Publisher for leader positions
        self.positions_pub = self.create_publisher(
            Int32MultiArray,
            '/leader_positions',
            10
        )
        
        # Service for start/stop
        self.start_stop_service = self.create_service(
            SetBool,
            '/udp_leader_receiver/start',
            self.handle_start_stop
        )
        
        # UDP socket and thread control
        self.socket = None
        self.running = False
        self.receiver_thread = None
        self.lock = threading.Lock()
        
        # Statistics
        self.packet_count = 0
        self.error_count = 0
        self.last_log_time = time.time()
        self.last_timestamp = None
        
        # Sequence number tracking
        self.last_sequence = -1  # Initialize to -1 so first packet (seq 0) is accepted
        self.out_of_order_count = 0
        
        self.get_logger().info(f'UDP Leader Receiver initialized on port {self.port}')
        
        # Auto-start if configured
        if self.auto_start:
            self.start_receiver()
    
    def start_receiver(self):
        """Start the UDP receiver"""
        with self.lock:
            if self.running:
                self.get_logger().warn('UDP receiver already running')
                return False
            
            try:
                # Create UDP socket
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind(('', self.port))
                self.socket.settimeout(1.0)  # 1 second timeout for clean shutdown
                
                # Start receiver thread
                self.running = True
                self.receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
                self.receiver_thread.start()
                
                # Reset sequence tracking on new connection
                self.last_sequence = -1
                self.out_of_order_count = 0
                
                self.get_logger().info(f'UDP receiver started on port {self.port}')
                return True
                
            except Exception as e:
                self.get_logger().error(f'Failed to start UDP receiver: {str(e)}')
                self.running = False
                if self.socket:
                    self.socket.close()
                    self.socket = None
                return False
    
    def stop_receiver(self):
        """Stop the UDP receiver"""
        with self.lock:
            if not self.running:
                self.get_logger().warn('UDP receiver not running')
                return False
            
            self.running = False
        
        # Wait for thread to finish (outside lock to avoid deadlock)
        if self.receiver_thread:
            self.receiver_thread.join(timeout=2.0)
            self.receiver_thread = None
        
        # Close socket
        if self.socket:
            self.socket.close()
            self.socket = None
        
        self.get_logger().info('UDP receiver stopped')
        return True
    
    def _receiver_loop(self):
        """Main receiver loop (runs in separate thread)"""
        self.get_logger().info('UDP receiver loop started')
        
        while self.running:
            try:
                # Receive data with timeout
                data, addr = self.socket.recvfrom(self.buffer_size)
                
                # Process the packet
                self._process_packet(data, addr)
                
            except socket.timeout:
                # Timeout is normal, just continue
                continue
                
            except Exception as e:
                if self.running:  # Only log if we're still supposed to be running
                    self.get_logger().error(f'Error receiving UDP data: {str(e)}')
                    self.error_count += 1
        
        self.get_logger().info('UDP receiver loop ended')
    
    def _process_packet(self, data: bytes, addr: tuple):
        """Process a received UDP packet"""
        # Check packet size
        if len(data) != self.PACKET_SIZE:
            self.get_logger().warn(
                f'Invalid packet size: {len(data)} bytes (expected {self.PACKET_SIZE}) from {addr}'
            )
            self.error_count += 1
            return
        
        try:
            # Unpack the entire packet
            unpacked = struct.unpack(self.FULL_PACKET_FORMAT, data)
            
            # Extract fields
            magic_header = unpacked[0]
            sequence = unpacked[1]
            timestamp = unpacked[2]
            servo_positions = list(unpacked[3:9])
            
            # Validate magic header
            if magic_header != self.MAGIC_HEADER:
                self.get_logger().warn(
                    f'Invalid magic header: 0x{magic_header:04X} (expected 0x{self.MAGIC_HEADER:04X}) from {addr}'
                )
                self.error_count += 1
                return
            
            # Check sequence number (handle wrap-around)
            if self._is_out_of_order(sequence):
                self.out_of_order_count += 1
                self.get_logger().debug(
                    f'Discarding out-of-order packet: seq={sequence} (last={self.last_sequence}) from {addr}'
                )
                return
            
            # Update last sequence number
            self.last_sequence = sequence
            
            # Publish the positions
            msg = Int32MultiArray()
            msg.data = servo_positions
            self.positions_pub.publish(msg)
            
            # Update statistics
            self.packet_count += 1
            self.last_timestamp = timestamp
            
            # Periodic logging
            current_time = time.time()
            if current_time - self.last_log_time >= self.log_rate:
                self.get_logger().info(
                    f'Stats - Packets: {self.packet_count}, Errors: {self.error_count}, '
                    f'Out-of-order: {self.out_of_order_count}, Last seq: {self.last_sequence}, '
                    f'Last timestamp: {timestamp:.2f}ms, Positions: {servo_positions}'
                )
                self.last_log_time = current_time
            
        except struct.error as e:
            self.get_logger().error(f'Failed to unpack packet from {addr}: {str(e)}')
            self.error_count += 1
        except Exception as e:
            self.get_logger().error(f'Error processing packet from {addr}: {str(e)}')
            self.error_count += 1
    
    def _is_out_of_order(self, sequence: int) -> bool:
        """Check if a sequence number is out of order (≤ last received)
        
        Handles 32-bit wrap-around correctly for long-running sessions.
        """
        if self.last_sequence == -1:
            # First packet
            return False
        
        # Handle 32-bit wrap-around
        # If sequence <= last_sequence, it's out of order or a duplicate
        # This works correctly with wrap-around because we only reject
        # packets that are not strictly newer
        return sequence <= self.last_sequence
    
    def handle_start_stop(self, request, response):
        """Handle start/stop service requests"""
        if request.data:
            # Start request
            success = self.start_receiver()
            response.success = success
            response.message = 'UDP receiver started' if success else 'Failed to start UDP receiver'
        else:
            # Stop request
            success = self.stop_receiver()
            response.success = success
            response.message = 'UDP receiver stopped' if success else 'Failed to stop UDP receiver'
        
        return response
    
    def destroy_node(self):
        """Cleanup when node is destroyed"""
        self.stop_receiver()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = UdpLeaderReceiver()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

