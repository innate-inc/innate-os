#!/usr/bin/env python3
"""
Enhanced I2C Motor Control Script for Jetson Orin Nano
30Hz continuous communication with bidirectional feedback
Commands: WASD for movement, space to stop, additional controls for LEDs/status
"""

import smbus2 as smbus
import time
import sys
import struct
import threading
from datetime import datetime

# I2C Configuration
I2C_BUS = 1           # Jetson I2C Bus 1 (pins 27/28)
MCU_ADDR = 0x42       # MCU slave address (matches your MCU code)

# Command definitions (Jetson → MCU)
CMD_MOVE = 0x01
CMD_STATUS = 0x03
CMD_CALIBRATE = 0x04

# Response definitions (MCU → Jetson)
RESP_MOVE = 0x81      # Position feedback
RESP_STATUS = 0x83    # Health data
RESP_CALIBRATE = 0x84 # Calibration status

# Movement parameters
MAX_SPEED = 100       # Max speed in cm/s (1.0 m/s * 100)
MAX_TURN = 256 / 2    # Max turn rate in rad/s * 100

class I2CMotorController:
    def __init__(self, debug=False, update_frequency=30.0, speed_command_timeout=5.0):
        self.debug = debug
        self.update_frequency = update_frequency
        self.speed_command_timeout = speed_command_timeout
        
        # Initialize I2C bus
        try:
            self.bus = smbus.SMBus(I2C_BUS)
            print(f"Connected to I2C bus {I2C_BUS}")
        except Exception as e:
            print(f"Failed to connect to I2C bus: {e}")
            sys.exit(1)
        
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
        self.current_position = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self.battery_voltage = 0.0
        self.motor_temperature = 0.0
        self.fault_code = 0
        self.calibration_status = None
        
        # Communication thread control
        self.running = True
        self.comm_thread = threading.Thread(target=self._communication_loop, daemon=True)
        self.comm_thread.start()
        
        print(f"Enhanced I2C Motor Controller initialized at 0x{MCU_ADDR:02X}")
        print(f"Running at {update_frequency}Hz with {speed_command_timeout}s timeout")

    def calculate_crc8_maxim(self, data):
        """Calculate CRC-8/MAXIM checksum"""
        crc = 0x00
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x01:
                    crc = (crc >> 1) ^ 0x8C
                else:
                    crc >>= 1
        return crc

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
            crc = self.calculate_crc8_maxim(message)
            message.append(crc)
            
            # Send via I2C
            self.bus.write_i2c_block_data(MCU_ADDR, 0x00, list(message))
            return True
        except Exception as e:
            if self.debug:
                print(f"Failed to send command 0x{cmd_id:02X}: {e}")
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
            data = self.bus.read_i2c_block_data(MCU_ADDR, 0x00, 8)
            
            if len(data) != 8:
                return None
            
            # Check if response is empty (all zeros or first byte is 0)
            if data[0] == 0x00:
                if self.debug:
                    print("Received empty response from MCU")
                return None
            
            # Verify CRC
            message = data[:7]
            received_crc = data[7]
            calculated_crc = self.calculate_crc8_maxim(message)
            
            if received_crc != calculated_crc:
                if self.debug:
                    print(f"CRC mismatch: expected 0x{calculated_crc:02X}, got 0x{received_crc:02X}")
                return None
            
            resp_id = data[0]
            response_data = data[1:7]
            
            self._process_response(resp_id, response_data)
            return True
            
        except Exception as e:
            if self.debug:
                print(f"Failed to read response: {e}")
            return None

    def _process_response(self, resp_id, data):
        """Process received response from MCU"""
        if self.debug:
            print(f"Received response 0x{resp_id:02X}: {[hex(b) for b in data]}")
        
        if resp_id == RESP_MOVE:
            # Position feedback: x, y (cm), theta (rad*100)
            try:
                x, y, theta = struct.unpack(">hhh", bytes(data))
                self.current_position = {
                    "x": x / 100.0,  # Convert to meters
                    "y": y / 100.0,  # Convert to meters
                    "theta": theta / 100.0  # Convert to radians
                }
                if self.debug:
                    print(f"Position: x={self.current_position['x']:.2f}m, "
                          f"y={self.current_position['y']:.2f}m, "
                          f"θ={self.current_position['theta']:.2f}rad")
            except struct.error as e:
                print(f"Failed to unpack position data: {e}")
                
        elif resp_id == RESP_STATUS:
            # Health status: battery voltage, motor temp, fault code
            try:
                battery, temp, fault, _ = struct.unpack(">HHBB", bytes(data))
                self.battery_voltage = battery / 100.0  # Convert to volts
                self.motor_temperature = temp  # Temperature in Celsius
                self.fault_code = fault
                print(f"Status - Battery: {self.battery_voltage:.2f}V, "
                      f"Motor Temp: {self.motor_temperature}°C, "
                      f"Fault: {self.fault_code}")
            except struct.error as e:
                print(f"Failed to unpack status data: {e}")
                
        elif resp_id == RESP_CALIBRATE:
            # Calibration status
            try:
                status = data[0]
                self.calibration_status = status
                status_str = {0: "Success", 1: "In Progress", 2: "Failure"}.get(status, "Unknown")
                print(f"Calibration Status: {status_str} ({status})")
            except Exception as e:
                print(f"Failed to process calibration data: {e}")
        else:
            print(f"Unknown response ID: 0x{resp_id:02X}")

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
        turn_int = int(max(-32767, min(32767, turn)))
        
        # Pack: speed (2 bytes), turn (2 bytes), reserved (2 bytes)
        data = struct.pack(">hhH", speed_int, turn_int, 0x0000)
        return self._send_command(CMD_MOVE, data)

    def _send_led_command(self):
        """LED functionality removed - placeholder for future use"""
        return False

    def _send_status_request(self):
        """Send status request if pending"""
        if not self.status_requested:
            return False
        
        # All bytes reserved (zero)
        data = bytes([0x00] * 6)
        success = self._send_command(CMD_STATUS, data)
        if success:
            self.status_requested = False  # Clear after sending
        return success

    def _send_calibrate_command(self):
        """Send calibration command if pending"""
        if not self.calibration_requested:
            return False
        
        # All bytes reserved (zero)
        data = bytes([0x00] * 6)
        success = self._send_command(CMD_CALIBRATE, data)
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
    def set_speed_command(self, speed, turn):
        """Set movement command (speed in m/s, turn in rad/s * 100)"""
        self.latest_speed = (speed, turn)
        self.last_speed_command_time = time.time()

    def set_led_command(self, mode, r, g, b, interval=1000):
        """LED functionality removed - placeholder for future use"""
        pass

    def request_status(self):
        """Request health status"""
        self.status_requested = True

    def request_calibration(self):
        """Request calibration"""
        self.calibration_requested = True

    def get_position(self):
        """Get current position estimate"""
        return self.current_position.copy()

    def get_status(self):
        """Get current health status"""
        return {
            "battery_voltage": self.battery_voltage,
            "motor_temperature": self.motor_temperature,
            "fault_code": self.fault_code
        }

    def stop(self):
        """Stop the controller and communication thread"""
        self.running = False
        # Send final stop command
        self.set_speed_command(0.0, 0.0)
        time.sleep(0.1)
        self.bus.close()

def main():
    print("Enhanced I2C Motor Control for Jetson Orin Nano")
    print("=" * 50)
    
    # Initialize controller
    controller = I2CMotorController(debug=False, update_frequency=30.0)
    
    print("\nSimple Controls (type command + Enter):")
    print("Movement:")
    print("  w = Forward       s = Backward")
    print("  a = Turn Left     d = Turn Right")
    print("  q = Forward+Left  e = Forward+Right")
    print("  z = Backward+Left c = Backward+Right")
    print("  space = Stop")
    print("\nSystem:")
    print("  h = Request Health Status")
    print("  cal = Request Calibration")
    print("  pos = Show Current Position")
    print("  x = Exit")
    print("\nType commands:")
    
    try:
        while True:
            try:
                command = input("> ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting...")
                break
            
            if not command:
                continue
            
            # Process commands
            if command in ['x', 'exit', 'quit']:
                print("Exiting...")
                break
                
            elif command == 'w':  # Forward
                controller.set_speed_command(1.0, 0.0)
                print("Moving Forward")
                
            elif command == 's':  # Backward
                controller.set_speed_command(-1.0, 0.0)
                print("Moving Backward")
                
            elif command == 'a':  # Turn left
                controller.set_speed_command(0.0, MAX_TURN)
                print("Turning Left")
                
            elif command == 'd':  # Turn right
                controller.set_speed_command(0.0, -MAX_TURN)
                print("Turning Right")
                
            elif command == 'q':  # Forward + Left
                controller.set_speed_command(1.0, MAX_TURN)
                print("Moving Forward + Left")
                
            elif command == 'e':  # Forward + Right
                controller.set_speed_command(1.0, -MAX_TURN)
                print("Moving Forward + Right")
                
            elif command == 'z':  # Backward + Left
                controller.set_speed_command(-1.0, MAX_TURN)
                print("Moving Backward + Left")
                
            elif command == 'c':  # Backward + Right
                controller.set_speed_command(-1.0, -MAX_TURN)
                print("Moving Backward + Right")
                
            elif command in ['space', 'stop', '']:  # Stop
                controller.set_speed_command(0.0, 0.0)
                print("STOP")
                
            elif command == 'h':  # Health status
                controller.request_status()
                print("Health status requested...")
                
            elif command == 'cal':  # Calibration
                controller.request_calibration()
                print("Calibration requested...")
                
            elif command == 'pos':  # Show position
                pos = controller.get_position()
                print(f"Position: x={pos['x']:.2f}m, y={pos['y']:.2f}m, θ={pos['theta']:.2f}rad")
                
            else:
                print("Invalid command. Use w/a/s/d for movement, h for status, x to exit")
    
    except Exception as e:
        print(f"Error: {e}")
    finally:
        controller.stop()
        print("Controller stopped")

if __name__ == "__main__":
    main()