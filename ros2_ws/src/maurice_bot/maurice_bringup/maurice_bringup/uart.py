#!/usr/bin/env python3
import rclpy
import serial
import struct
import time
import threading

from rclpy.node import Node
from geometry_msgs.msg import TransformStamped

# CRC-8/MAXIM constants
CRC8_POLY = 0x8C
CRC8_INIT = 0x00

class UartManager:
    SOM_MARKER = bytes([0x69, 0x69])  # Fixed 2-byte start-of-message

    # Command IDs (sent from main computer to microcontroller)
    CMD_MOVE   = 0x01
    CMD_LED    = 0x02
    CMD_STATUS = 0x03

    # Response IDs (microcontroller replies with CMD+0x80)
    RESP_MOVE   = 0x81  # Position update
    RESP_LED    = 0x82  # LED status feedback
    RESP_STATUS = 0x83  # Health (status) feedback

    def __init__(self, node: Node, port='/dev/ttyTHS1', baud_rate=115200, timeout=0.1, update_frequency=30.0, debug=False):
        self.node = node
        self.debug = debug
        self.logger = self.node.get_logger()
        
        # -------------------------
        # Stored command values
        # -------------------------
        self.latest_speed = (0.0, 0.0)  # (forward_speed, turn_rate)
        # LED command stored as a tuple (mode, interval, r, g, b);
        # When None, no new LED command is pending.
        self.latest_led = None
        # Flag indicating a new status request is pending.
        self.status_requested = False

        # -------------------------
        # Stored responses
        # -------------------------
        self.current_transform = TransformStamped()  # Latest position feedback
        self.current_led_status = None                # Latest LED status (dictionary)
        self.battery_voltage = 0.0                      # Latest battery voltage
        self.motor_temperature = 0.0                    # Latest motor temperature
        self.fault_code = 0                           # Latest fault code

        # -------------------------
        # UART configuration
        # -------------------------
        try:
            self.ser = serial.Serial(port, baudrate=baud_rate, timeout=timeout)
            self.logger.info(f"Connected to {port} at {baud_rate} baud")
        except serial.SerialException as e:
            self.logger.error(f"Failed to open serial port: {e}")
            raise

        # Buffer for incoming bytes (for sliding window processing)
        self._rx_buffer = bytearray()

        # Start the fixed-rate communication loop at 50Hz in a separate thread
        self.running = True
        self.update_frequency = update_frequency
        self.comm_thread = threading.Thread(target=self._communication_loop, daemon=True)
        self.comm_thread.start()

    # --- CRC Calculation (CRC-8/MAXIM, LSB-first) ---
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

    # --- Low-Level Command Sending ---
    def _send_command(self, cmd_id: int, data: bytes):
        """
        Build and send a 10-byte command.
        Packet layout:
          [SOM (2 bytes)] + [cmd_id (1 byte)] + [data (6 bytes)] + [CRC (1 byte)]
        """
        if len(data) != 6:
            raise ValueError("Payload data must be exactly 6 bytes.")
        protocol_msg = bytes([cmd_id]) + data  # 7 bytes: command ID + data
        crc = self._calculate_crc(protocol_msg)
        packet = self.SOM_MARKER + protocol_msg + bytes([crc])
        
        # Only log LED commands
        # if cmd_id == self.CMD_LED:
        #     self.logger.info(f"Sending LED command: {packet.hex(' ')}")
        #     if self.debug:
        #         self.logger.debug(f"  SOM: {self.SOM_MARKER.hex(' ')}")
        #         self.logger.debug(f"  CMD: {cmd_id:02X}")
        #         self.logger.debug(f"  Data: {data.hex(' ')}")
        #         self.logger.debug(f"  CRC: {crc:02X}")
        
        self.ser.write(packet)

    def _send_speed_command(self):
        forward_speed, turn_rate = self.latest_speed
        speed_int = int(forward_speed * 100)
        turn_int = int(turn_rate * 100)
        # Pack: 2 bytes forward speed, 2 bytes turn rate, 2 bytes reserved (0)
        payload = struct.pack(">hhH", speed_int, turn_int, 0x0000)
        self._send_command(self.CMD_MOVE, payload)

    def _send_led_command(self):
        # latest_led is a tuple: (mode, interval, r, g, b)
        mode, interval, r, g, b = self.latest_led
        # Pack: 1 byte mode, 2 bytes interval, 1 byte red, 1 byte green, 1 byte blue
        payload = struct.pack(">B H B B B", mode, interval, r, g, b)
        self._send_command(self.CMD_LED, payload)

    def _send_status_request(self):
        # All 6 bytes reserved (zero)
        payload = bytes([0x00] * 6)
        self._send_command(self.CMD_STATUS, payload)

    # --- Communication Loop (50Hz) ---
    def _communication_loop(self):
        while self.running:
            # Always send the speed command
            self._send_speed_command()
            self._read_response()  # Process any incoming response (e.g. position feedback)

            # Send LED command only if a new command is pending
            if self.latest_led is not None:
                # self.logger.info("Sending LED command")
                self._send_led_command()
                self._read_response()  # Process LED status feedback
                self.latest_led = None  # Reset after sending
                # self.logger.info("LED command sent and processed")

            # Send status request only if one was triggered
            if self.status_requested:
                self._send_status_request()
                self._read_response()  # Process health feedback
                self.status_requested = False

            time.sleep(1 / self.update_frequency)  # 50Hz loop

    def _read_response(self):
        """
        Reads all available bytes from the serial port into self._rx_buffer and processes
        as many complete packets as possible using an FSM.
        
        Packet format:
          [SOM (2 bytes)] + [protocol (7 bytes: response ID + data)] + [CRC (1 byte)]
        
        This version will, once the second SOM is found, check if enough bytes remain in the
        buffer to read the rest of the packet. If not, it stops processing and leaves the
        partial packet in self._rx_buffer for later.
        Reads all available bytes from the serial port into self._rx_buffer and processes
        as many complete packets as possible using an FSM.
        
        Packet format:
          [SOM (2 bytes)] + [protocol (7 bytes: response ID + data)] + [CRC (1 byte)]
        
        This version will, once the second SOM is found, check if enough bytes remain in the
        buffer to read the rest of the packet. If not, it stops processing and leaves the
        partial packet in self._rx_buffer for later.
        """
        try:
            # Append all available bytes into the persistent buffer.
            # Append all available bytes into the persistent buffer.
            bytes_available = self.ser.in_waiting
            if bytes_available:
                self._rx_buffer.extend(self.ser.read(bytes_available))

            # Define FSM states.
            WAIT_FOR_FIRST_SOM = 0
            WAIT_FOR_SECOND_SOM = 1
            READ_PROTOCOL = 2    # Read 7 bytes: response ID (1) + data (6)
            READ_CRC = 3         # Read 1 byte CRC

            state = WAIT_FOR_FIRST_SOM
            packet = bytearray()  # Temporary storage for the current packet
            index = 0             # Index into self._rx_buffer

            # Process bytes in the buffer.
            while index < len(self._rx_buffer):
                if state == WAIT_FOR_FIRST_SOM:
                    # Look for first SOM byte.
                    if self._rx_buffer[index] == 0x69:
                        packet = bytearray([0x69])
                        state = WAIT_FOR_SECOND_SOM
                    index += 1

                elif state == WAIT_FOR_SECOND_SOM:
                    # We expect the next byte to be 0x69.
                    if self._rx_buffer[index] == 0x69:
                        packet.append(0x69)
                        state = READ_PROTOCOL
                        index += 1
                    else:
                        # Not a valid second SOM. Reset and continue searching.
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                        index += 1

                elif state == READ_PROTOCOL:
                    # The protocol portion is 7 bytes.
                    # (We already have 2 SOM bytes, so we need 7 more bytes.)
                    bytes_needed = 7 - (len(packet) - 2)  # How many bytes are missing from protocol part.
                    if (len(self._rx_buffer) - index) >= bytes_needed:
                        # Enough bytes available: read them all at once.
                        packet.extend(self._rx_buffer[index:index + bytes_needed])
                        index += bytes_needed
                        state = READ_CRC
                    else:
                        # Not enough bytes available to complete protocol message.
                        break

                elif state == READ_CRC:
                    # Need 1 byte for CRC.
                    if (len(self._rx_buffer) - index) >= 1:
                        packet.append(self._rx_buffer[index])
                        index += 1
                        # Now the packet should be complete (10 bytes total)
                        if len(packet) == 10:
                            protocol_msg = packet[2:9]  # 7-byte protocol message (response ID + data)
                            computed_crc = self._calculate_crc(protocol_msg)
                            received_crc = packet[9]
                            if computed_crc == received_crc:
                                msg_id = protocol_msg[0]
                                data_field = protocol_msg[1:]
                                self._process_response(msg_id, data_field)
                            else:
                                self.logger.error(
                                    f"CRC mismatch in FSM: expected {computed_crc:02X}, got {received_crc:02X}"
                                )
                        # Reset state to look for the next packet.
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                    else:
                        # Not enough bytes available to read the CRC.
                        break

            # Save any leftover (incomplete) data back into _rx_buffer.
            self._rx_buffer = self._rx_buffer[index:]

            # Define FSM states.
            WAIT_FOR_FIRST_SOM = 0
            WAIT_FOR_SECOND_SOM = 1
            READ_PROTOCOL = 2    # Read 7 bytes: response ID (1) + data (6)
            READ_CRC = 3         # Read 1 byte CRC

            state = WAIT_FOR_FIRST_SOM
            packet = bytearray()  # Temporary storage for the current packet
            index = 0             # Index into self._rx_buffer

            # Process bytes in the buffer.
            while index < len(self._rx_buffer):
                if state == WAIT_FOR_FIRST_SOM:
                    # Look for first SOM byte.
                    if self._rx_buffer[index] == 0x69:
                        packet = bytearray([0x69])
                        state = WAIT_FOR_SECOND_SOM
                    index += 1

                elif state == WAIT_FOR_SECOND_SOM:
                    # We expect the next byte to be 0x69.
                    if self._rx_buffer[index] == 0x69:
                        packet.append(0x69)
                        state = READ_PROTOCOL
                        index += 1
                    else:
                        # Not a valid second SOM. Reset and continue searching.
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                        index += 1

                elif state == READ_PROTOCOL:
                    # The protocol portion is 7 bytes.
                    # (We already have 2 SOM bytes, so we need 7 more bytes.)
                    bytes_needed = 7 - (len(packet) - 2)  # How many bytes are missing from protocol part.
                    if (len(self._rx_buffer) - index) >= bytes_needed:
                        # Enough bytes available: read them all at once.
                        packet.extend(self._rx_buffer[index:index + bytes_needed])
                        index += bytes_needed
                        state = READ_CRC
                    else:
                        # Not enough bytes available to complete protocol message.
                        break

                elif state == READ_CRC:
                    # Need 1 byte for CRC.
                    if (len(self._rx_buffer) - index) >= 1:
                        packet.append(self._rx_buffer[index])
                        index += 1
                        # Now the packet should be complete (10 bytes total)
                        if len(packet) == 10:
                            protocol_msg = packet[2:9]  # 7-byte protocol message (response ID + data)
                            computed_crc = self._calculate_crc(protocol_msg)
                            received_crc = packet[9]
                            if computed_crc == received_crc:
                                msg_id = protocol_msg[0]
                                data_field = protocol_msg[1:]
                                self._process_response(msg_id, data_field)
                            else:
                                self.logger.error(
                                    f"CRC mismatch in FSM: expected {computed_crc:02X}, got {received_crc:02X}"
                                )
                        # Reset state to look for the next packet.
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                    else:
                        # Not enough bytes available to read the CRC.
                        break

            # Save any leftover (incomplete) data back into _rx_buffer.
            self._rx_buffer = self._rx_buffer[index:]
        except Exception as e:
            self.logger.error(f"Error in FSM processing: {e}")

            self.logger.error(f"Error in FSM processing: {e}")


    # --- Processing Responses ---
    def _process_response(self, msg_id: int, data: bytes):
        # Add debug logging for received message
        if self.debug:
            self.logger.debug(f"Received UART message - ID: 0x{msg_id:02X}, Data: {data.hex(' ')}")
        
        if msg_id == self.RESP_MOVE:
            try:
                x, y, theta = struct.unpack(">hhh", data)
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
            self.logger.info(f"Position Update - X: {x/100.0}, Y: {y/100.0}, θ: {theta_rad} rad")
            self.logger.info(f"Position Update - X: {x/100.0}, Y: {y/100.0}, θ: {theta_rad} rad")
        elif msg_id == self.RESP_LED:
            try:
                mode, red, green, blue, interval = struct.unpack(">B B B B H", data)
            except struct.error as e:
                self.logger.error(f"Failed to unpack LED response: {e}")
                return
            self.current_led_status = {
                "mode": mode,
                "red": red,
                "green": green,
                "blue": blue,
                "interval": interval
            }
            if self.debug:
                self.logger.debug(f"LED Status - Mode: {mode}, RGB: ({red},{green},{blue}), Interval: {interval}ms")
            self.logger.info(f"LED Status - Mode: {mode}, RGB: ({red},{green},{blue}), Interval: {interval}ms")
            self.logger.info(f"LED Status - Mode: {mode}, RGB: ({red},{green},{blue}), Interval: {interval}ms")
        elif msg_id == self.RESP_STATUS:
            try:
                batt, temp, fault, _ = struct.unpack(">H H B B", data)
            except struct.error as e:
                self.logger.error(f"Failed to unpack status response: {e}")
                return
            self.battery_voltage = batt / 100.0
            self.motor_temperature = temp
            self.fault_code = fault
            if self.debug:
                self.logger.debug(f"Status - Battery: {self.battery_voltage}V, Motor Temp: {temp}°C, Fault: {fault}")
            self.logger.info(f"Status - Battery: {self.battery_voltage}V, Motor Temp: {temp}°C, Fault: {fault}")
            self.logger.info(f"Status - Battery: {self.battery_voltage}V, Motor Temp: {temp}°C, Fault: {fault}")
        else:
            self.logger.warning(f"Unknown Response ID: {msg_id}")

    # --- Public Interface Functions (Same as old UART API, with updated LED interface) ---
    def set_speed_command(self, v: float, omega: float):
        """
        Sets the desired linear and angular velocities.
          v: linear speed in m/s (e.g., ±1.28)
          omega: angular speed in rad/s (e.g., ±2.56)
        """
        self.latest_speed = (v, omega)
    def set_light_command(self, mode: int, r: int, g: int, b: int, interval: int = 1000):
        """
        Sets the LED command.
          mode: LED mode (0 = Off, 1 = Solid, 2 = Blink, 3 = Ring, etc.)
          r, g, b: Color intensities (0-255)
          interval: Time parameter in milliseconds (1-10000) for modes that require an interval.
        """
        # self.logger.info(f"Setting LED command - Mode: {mode}, RGB: ({r},{g},{b}), Interval: {interval}ms")
        self.latest_led = (mode, interval, r, g, b)

    def request_health(self):
        """
        Triggers a status request to obtain health feedback (battery voltage, motor temperature, fault code).
        """
        self.status_requested = True

    # --- Destructor ---
    def __del__(self):
        self.running = False
        time.sleep(0.1)  # Give the communication loop time to exit
        if hasattr(self, 'ser') and self.ser:
            self.ser.close()
