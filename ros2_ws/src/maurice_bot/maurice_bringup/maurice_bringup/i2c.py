#!/usr/bin/env python3
"""
Enhanced I2C Motor Control Script for Jetson Orin Nano
30Hz continuous communication with bidirectional feedback
Commands: WASD for movement, space to stop, additional controls for LEDs/status
"""

import smbus2 as smbus
import time
import struct
import threading

from rclpy.node import Node
from geometry_msgs.msg import TransformStamped

# CRC-8/MAXIM constants
CRC8_POLY = 0x8C
CRC8_INIT = 0x00

class I2CManager:
    # Command definitions (Jetson → MCU)
    CMD_MOVE = 0x01
    CMD_STATUS = 0x03
    CMD_CALIBRATE = 0x04

    # Response definitions (MCU → Jetson)
    RESP_MOVE = 0x81      # Position feedback
    RESP_STATUS = 0x83    # Health data
    RESP_CALIBRATE = 0x84 # Calibration status
    def __init__(self, node: Node, bus_number=1, device_address=0x42, update_frequency=30.0, debug=False, speed_command_timeout=5.0):
        self.node = node
        self.debug = debug
        self.logger = self.node.get_logger()
        self.update_frequency = update_frequency
        self.speed_command_timeout = speed_command_timeout
        self.bus_number = bus_number
        self.device_address = device_address
        
        # Initialize I2C bus
        try:
            self.bus = smbus.SMBus(bus_number)
            self.logger.info(f"Connected to I2C bus {bus_number} at address 0x{device_address:02X}")
        except Exception as e:
            self.logger.error(f"Failed to connect to I2C bus {bus_number}: {e}")
            raise
        
        # -------------------------
        # Stored command values
        # -------------------------
        self.latest_speed = (0.0, 0.0)  # (forward_speed, turn_rate)
        self.last_speed_command_time = 0.0
        self.status_requested = False
        self.calibration_requested = False
        
        # -------------------------
        # Stored responses
        # -------------------------
        # Initialize zero transform for odom->base_link using a dedicated method.
        self.current_transform = self._initialize_transform()
        self.battery_voltage = 0.0
        self.motor_temperature = 0.0
        self.fault_code = 0
        self.calibration_status = None
        
        # Communication thread control
        self.running = True
        self.comm_thread = threading.Thread(target=self._communication_loop, daemon=True)
        self.comm_thread.start()

    def _calculate_crc(self, data: bytes) -> int:
        crc = CRC8_INIT
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x01:
                    crc = (crc >> 1) ^ CRC8_POLY
                else:
                    crc >>= 1
        return crc

    def _initialize_transform(self) -> TransformStamped:
        """
        Initializes and returns a zero (identity) transform from "odom" to "base_link".
        """
        transform = TransformStamped()
        transform.header.frame_id = "odom"
        transform.child_frame_id = "base_link"
        transform.transform.translation.x = 0.0
        transform.transform.translation.y = 0.0
        transform.transform.translation.z = 0.0
        transform.transform.rotation.x = 0.0
        transform.transform.rotation.y = 0.0
        transform.transform.rotation.z = 0.0
        transform.transform.rotation.w = 1.0
        return transform

    def _send_command(self, cmd_id, data_bytes):
        """
        Send 8-byte command to MCU via I2C
        Format: [cmd_id] + [6 data bytes] + [crc]
        """
        if len(data_bytes) != 6:
            raise ValueError("Data must be exactly 6 bytes")
        
        try:
            # Build 8-byte message
            message = bytearray([cmd_id]) + bytearray(data_bytes)
            crc = self._calculate_crc(message)
            message.append(crc)
            
            # Send via I2C
            self.bus.write_i2c_block_data(self.device_address, 0x00, list(message))
            return True
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Failed to send command 0x{cmd_id:02X}: {e}")
            return False

    def _read_response(self):
        """
        Read 8-byte response from MCU via I2C
        Format: [resp_id] + [6 data bytes] + [crc]
        """
        try:
            # Small delay to allow MCU to prepare response
            time.sleep(0.001)  # 1ms delay
            
            # Read 8 bytes from MCU
            data = self.bus.read_i2c_block_data(self.device_address, 0x00, 8)
            
            if len(data) != 8:
                return None
            
            # Check if response is empty (all zeros or first byte is 0)
            if data[0] == 0x00:
                if self.debug:
                    self.logger.debug("Received empty response from MCU")
                return None
            
            # Verify CRC
            message = data[:7]
            received_crc = data[7]
            calculated_crc = self._calculate_crc(message)
            
            if received_crc != calculated_crc:
                if self.debug:
                    self.logger.debug(f"CRC mismatch: expected 0x{calculated_crc:02X}, got 0x{received_crc:02X}")
                return None
            
            resp_id = data[0]
            response_data = data[1:7]
            
            self._process_response(resp_id, response_data)
            return True
            
        except Exception as e:
            if self.debug:
                self.logger.debug(f"Failed to read response: {e}")
            return None

    def _process_response(self, resp_id, data):
        """Process received response from MCU"""
        if self.debug:
            self.logger.debug(f"Received response 0x{resp_id:02X}: {[hex(b) for b in data]}")
        
        if resp_id == self.RESP_MOVE:
            # Position feedback: x, y (cm), theta (rad*100)
            try:
                x, y, theta = struct.unpack(">hhh", bytes(data))
            except struct.error as e:
                self.logger.error(f"Failed to unpack movement response: {e}")
                return
            now = self.node.get_clock().now().to_msg()
            self.current_transform.header.stamp = now
            self.current_transform.header.frame_id = "odom"
            self.current_transform.child_frame_id = "base_link"
            self.current_transform.transform.translation.x = x / 100.0
            self.current_transform.transform.translation.y = y / 100.0
            self.current_transform.transform.translation.z = 0.0
            import math
            theta_rad = theta / 100.0
            self.current_transform.transform.rotation.x = 0.0
            self.current_transform.transform.rotation.y = 0.0
            self.current_transform.transform.rotation.z = math.sin(theta_rad / 2.0)
            self.current_transform.transform.rotation.w = math.cos(theta_rad / 2.0)
            if self.debug:
                self.logger.debug(f"Position Update - X: {x/100.0}, Y: {y/100.0}, θ: {theta_rad} rad")
        elif resp_id == self.RESP_STATUS:
            # Health status: battery voltage, motor temp, fault code
            try:
                battery, temp, fault, _ = struct.unpack(">HHBB", bytes(data))
                self.battery_voltage = battery / 100.0  # Convert to volts
                self.motor_temperature = temp  # Temperature in Celsius
                self.fault_code = fault
                if self.debug:
                    self.logger.debug(f"Status - Battery: {self.battery_voltage:.2f}V, Motor Temp: {temp}°C, Fault: {fault}")
                self.logger.info(f"Status - Battery: {self.battery_voltage:.2f}V, Motor Temp: {temp}°C, Fault: {fault}")
            except struct.error as e:
                self.logger.error(f"Failed to unpack status response: {e}")
                
        elif resp_id == self.RESP_CALIBRATE:
            # Calibration status
            try:
                status = data[0]
                self.calibration_status = status
                status_str = {0: "Success", 1: "In Progress", 2: "Failure"}.get(status, "Unknown")
                if self.debug:
                    self.logger.debug(f"Calibration Status: {status_str} ({status})")
                self.logger.info(f"Calibration Status: {status_str} ({status})")
            except Exception as e:
                self.logger.error(f"Failed to unpack calibration response: {e}")
        else:
            self.logger.warning(f"Unknown Response ID: {resp_id}")

    def _send_move_command(self):
        """Send movement command with timeout handling"""
        current_time = time.time()
        if current_time - self.last_speed_command_time > self.speed_command_timeout:
            # Timeout exceeded, send zero speed
            speed, turn = 0.0, 0.0
        else:
            speed, turn = self.latest_speed
        
        # Scale and clamp values
        speed_int = int(max(-32767, min(32767, speed * 100)))
        turn_int = int(max(-32767, min(32767, turn * 100)))
        
        # Pack: speed (2 bytes), turn (2 bytes), reserved (2 bytes)
        data = struct.pack(">hhH", speed_int, turn_int, 0x0000)
        return self._send_command(self.CMD_MOVE, data)

    def _send_led_command(self):
        """LED functionality removed - placeholder for future use"""
        return False

    def _send_status_request(self):
        """Send status request if pending"""
        if not self.status_requested:
            return False
        
        # All bytes reserved (zero)
        data = bytes([0x00] * 6)
        success = self._send_command(self.CMD_STATUS, data)
        if success:
            self.status_requested = False  # Clear after sending
        return success

    def _send_calibrate_command(self):
        """Send calibration command if pending"""
        if not self.calibration_requested:
            return False
        
        # All bytes reserved (zero)
        data = bytes([0x00] * 6)
        success = self._send_command(self.CMD_CALIBRATE, data)
        if success:
            self.calibration_requested = False  # Clear after sending
        return success

    def _communication_loop(self):
        """Main communication loop running at fixed frequency"""
        while self.running:
            loop_start = time.time()
            
            # Always send movement command
            self._send_move_command()
            self._read_response()  # Try to read position feedback
            
            # Send conditional commands
            if self.status_requested:
                self._send_status_request()
                self._read_response()
            
            if self.calibration_requested:
                self._send_calibrate_command()
                self._read_response()
            
            # Maintain fixed update rate
            elapsed = time.time() - loop_start
            sleep_time = (1.0 / self.update_frequency) - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    # Public interface functions
    def set_speed_command(self, v: float, omega: float):
        """
        Sets the desired linear and angular velocities.
          v: linear speed in m/s (e.g., ±1.28)
          omega: angular speed in rad/s (e.g., ±2.56)
        """
        self.latest_speed = (v, omega)
        self.last_speed_command_time = time.time()

    def set_light_command(self, mode: int, r: int, g: int, b: int, interval: int = 1000):
        """
        Sets the LED command.
          mode: LED mode (0 = Off, 1 = Solid, 2 = Blink, 3 = Ring, etc.)
          r, g, b: Color intensities (0-255)
          interval: Time parameter in milliseconds (1-10000) for modes that require an interval.
        """
        # LED functionality removed - placeholder for future use
        pass

    def request_health(self):
        """
        Triggers a status request to obtain health feedback (battery voltage, motor temperature, fault code).
        """
        self.status_requested = True

    def request_calibration(self):
        """
        Triggers a calibration command.
        """
        self.calibration_requested = True

    # --- Destructor ---
    def __del__(self):
        self.running = False
        time.sleep(0.1)  # Give the communication loop time to exit
        if hasattr(self, 'bus') and self.bus:
            self.bus.close()
