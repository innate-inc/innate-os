"""
Wheel controller for Maurice robot using UART communication.

This module provides a controller for the wheels of the Maurice robot,
communicating with the microcontroller via UART.
"""

import serial
import struct
import threading
import time


# CRC-8/MAXIM constants for UART communication
CRC8_POLY = 0x8C
CRC8_INIT = 0x00


class WheelController:
    """
    Controller for Maurice robot wheels using UART communication.
    This class handles the communication with the microcontroller that
    controls the wheels.
    """

    SOM_MARKER = bytes([0x69, 0x69])  # Fixed 2-byte start-of-message

    # Command IDs (sent from main computer to microcontroller)
    CMD_MOVE = 0x01
    CMD_LED = 0x02
    CMD_STATUS = 0x03

    # Response IDs (microcontroller replies with CMD+0x80)
    RESP_MOVE = 0x81  # Position update
    RESP_LED = 0x82  # LED status feedback
    RESP_STATUS = 0x83  # Health (status) feedback

    def __init__(
        self,
        port="/dev/ttyTHS1",
        baud_rate=115200,
        timeout=0.1,
        update_frequency=30.0,
        debug=False,
    ):
        """
        Initialize the wheel controller.

        Args:
            port: Serial port to use (default: /dev/ttyTHS1)
            baud_rate: Baud rate for serial communication (default: 115200)
            timeout: Serial read timeout in seconds (default: 0.1)
            update_frequency: Frequency of the communication loop in Hz (default: 30.0)
            debug: Whether to print debug messages (default: False)
        """
        self.debug = debug

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
        self.position = (0.0, 0.0, 0.0)  # (x, y, theta) in meters and radians
        self.current_led_status = None  # Latest LED status (dictionary)
        self.battery_voltage = 0.0  # Latest battery voltage
        self.motor_temperature = 0.0  # Latest motor temperature
        self.fault_code = 0  # Latest fault code

        # -------------------------
        # UART configuration
        # -------------------------
        try:
            self.ser = serial.Serial(port, baudrate=baud_rate, timeout=timeout)
            print(f"Connected to wheel controller on {port} at {baud_rate} baud")
        except serial.SerialException as e:
            print(f"Failed to open serial port: {e}")
            raise

        # Buffer for incoming bytes (for sliding window processing)
        self._rx_buffer = bytearray()

        # Start the fixed-rate communication loop in a separate thread
        self.running = True
        self.update_frequency = update_frequency
        self.comm_thread = threading.Thread(
            target=self._communication_loop, daemon=True
        )
        self.comm_thread.start()

    # --- CRC Calculation (CRC-8/MAXIM, LSB-first) ---
    def _calculate_crc(self, data: bytes) -> int:
        """
        Calculate CRC-8/MAXIM checksum.

        Args:
            data: Bytes to calculate CRC for

        Returns:
            Calculated CRC value
        """
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

        Args:
            cmd_id: Command ID to send
            data: 6 bytes of payload data
        """
        if len(data) != 6:
            raise ValueError("Payload data must be exactly 6 bytes.")
        protocol_msg = bytes([cmd_id]) + data  # 7 bytes: command ID + data
        crc = self._calculate_crc(protocol_msg)
        packet = self.SOM_MARKER + protocol_msg + bytes([crc])

        self.ser.write(packet)

    def _send_speed_command(self):
        """Send the current speed command to the wheels."""
        forward_speed, turn_rate = self.latest_speed
        speed_int = int(forward_speed * 100)
        turn_int = int(turn_rate * 100)
        # Pack: 2 bytes forward speed, 2 bytes turn rate, 2 bytes reserved (0)
        payload = struct.pack(">hhH", speed_int, turn_int, 0x0000)
        self._send_command(self.CMD_MOVE, payload)

    def _send_led_command(self):
        """Send the current LED command."""
        # latest_led is a tuple: (mode, interval, r, g, b)
        mode, interval, r, g, b = self.latest_led
        # Pack: 1 byte mode, 2 bytes interval, 1 byte red, 1 byte green,
        # 1 byte blue
        payload = struct.pack(">B H B B B", mode, interval, r, g, b)
        self._send_command(self.CMD_LED, payload)

    def _send_status_request(self):
        """Send a status request to get health information."""
        # All 6 bytes reserved (zero)
        payload = bytes([0x00] * 6)
        self._send_command(self.CMD_STATUS, payload)

    # --- Communication Loop ---
    def _communication_loop(self):
        """Main communication loop that runs in a separate thread."""
        while self.running:
            # Always send the speed command
            self._send_speed_command()
            self._read_response()  # Process any incoming response

            # Send LED command only if a new command is pending
            if self.latest_led is not None:
                self._send_led_command()
                self._read_response()  # Process LED status feedback
                self.latest_led = None  # Reset after sending

            # Send status request only if one was triggered
            if self.status_requested:
                self._send_status_request()
                self._read_response()  # Process health feedback
                self.status_requested = False

            time.sleep(1 / self.update_frequency)

    def _read_response(self):
        """
        Read and process responses from the microcontroller.
        Uses a finite state machine to parse the protocol.
        """
        try:
            # Append all available bytes from the serial port.
            bytes_available = self.ser.in_waiting
            if bytes_available:
                new_bytes = self.ser.read(bytes_available)
                self._rx_buffer.extend(new_bytes)

            # Define FSM states.
            WAIT_FOR_FIRST_SOM = 0
            WAIT_FOR_SECOND_SOM = 1
            READ_PROTOCOL = 2  # Read 7 bytes: response ID (1) + data (6)
            READ_CRC = 3  # Read 1 byte for CRC

            state = WAIT_FOR_FIRST_SOM
            packet = bytearray()  # Temporary storage for the current packet
            index = 0  # Current index in self._rx_buffer

            while index < len(self._rx_buffer):
                if state == WAIT_FOR_FIRST_SOM:
                    if self._rx_buffer[index] == 0x69:
                        packet = bytearray([0x69])
                        state = WAIT_FOR_SECOND_SOM
                    index += 1

                elif state == WAIT_FOR_SECOND_SOM:
                    if index >= len(self._rx_buffer):
                        break  # Wait for more data.
                    if self._rx_buffer[index] == 0x69:
                        packet.append(0x69)
                        state = READ_PROTOCOL
                        index += 1
                    else:
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                        index += 1

                elif state == READ_PROTOCOL:
                    bytes_needed = 7 - (len(packet) - 2)
                    if (len(self._rx_buffer) - index) >= bytes_needed:
                        packet.extend(self._rx_buffer[index : index + bytes_needed])
                        index += bytes_needed
                        state = READ_CRC
                    else:
                        break

                elif state == READ_CRC:
                    if (len(self._rx_buffer) - index) >= 1:
                        packet.append(self._rx_buffer[index])
                        index += 1
                        if len(packet) == 10:
                            # Validate packet.
                            protocol_msg = packet[2:9]  # 7-byte protocol
                            computed_crc = self._calculate_crc(protocol_msg)
                            received_crc = packet[9]
                            if computed_crc == received_crc:
                                msg_id = protocol_msg[0]
                                data_field = protocol_msg[1:]
                                self._process_response(msg_id, data_field)
                            else:
                                if self.debug:
                                    print(
                                        f"CRC mismatch: expected "
                                        f"{computed_crc:02X}, got "
                                        f"{received_crc:02X}"
                                    )
                        else:
                            if self.debug:
                                print("Packet size error: expected 10 bytes")
                        # Reset for next packet.
                        state = WAIT_FOR_FIRST_SOM
                        packet = bytearray()
                    else:
                        if self.debug:
                            print("Not enough bytes for CRC; waiting for more")
                        break

            # Clear processed bytes from the buffer.
            self._rx_buffer = self._rx_buffer[index:]
        except Exception as e:
            print(f"Error in _read_response FSM: {e}")

    # --- Processing Responses ---
    def _process_response(self, msg_id: int, data: bytes):
        """
        Process responses from the microcontroller.

        Args:
            msg_id: Response message ID
            data: Response data payload
        """
        # Add debug logging for received message
        if self.debug:
            print(
                f"Received UART message - ID: 0x{msg_id:02X}, " f"Data: {data.hex(' ')}"
            )

        if msg_id == self.RESP_MOVE:
            try:
                x, y, theta = struct.unpack(">hhh", data)
                # Convert to meters and radians
                x_m = x / 1000.0
                y_m = y / 1000.0
                theta_rad = theta / 100.0
                self.position = (x_m, y_m, theta_rad)
                if self.debug:
                    print(
                        f"Position Update - X: {x_m}, Y: {y_m}, " f"θ: {theta_rad} rad"
                    )
            except struct.error as e:
                print(f"Failed to unpack movement response: {e}")

        elif msg_id == self.RESP_LED:
            try:
                mode, red, green, blue, interval = struct.unpack(">B B B B H", data)
                self.current_led_status = {
                    "mode": mode,
                    "red": red,
                    "green": green,
                    "blue": blue,
                    "interval": interval,
                }
                if self.debug:
                    print(
                        f"LED Status - Mode: {mode}, "
                        f"RGB: ({red},{green},{blue}), "
                        f"Interval: {interval}ms"
                    )
            except struct.error as e:
                print(f"Failed to unpack LED response: {e}")

        elif msg_id == self.RESP_STATUS:
            try:
                batt, temp, fault, _ = struct.unpack(">H H B B", data)
                self.battery_voltage = batt / 100.0
                self.motor_temperature = temp
                self.fault_code = fault
                if self.debug:
                    print(
                        f"Status - Battery: {self.battery_voltage}V, "
                        f"Motor Temp: {temp}°C, Fault: {fault}"
                    )
            except struct.error as e:
                print(f"Failed to unpack status response: {e}")

        else:
            print(f"Unknown Response ID: {msg_id}")

    # --- Public Interface Functions ---
    def set_speed(self, v: float, omega: float):
        """
        Sets the desired linear and angular velocities.

        Args:
            v: Linear speed in m/s (e.g., ±1.28)
            omega: Angular speed in rad/s (e.g., ±2.56)
        """
        self.latest_speed = (v, omega)

    def set_led(self, mode: int, r: int, g: int, b: int, interval: int = 1000):
        """
        Sets the LED command.

        Args:
            mode: LED mode (0 = Off, 1 = Solid, 2 = Blink, 3 = Ring, etc.)
            r: Red intensity (0-255)
            g: Green intensity (0-255)
            b: Blue intensity (0-255)
            interval: Time parameter in milliseconds (1-10000) for modes that
                     require an interval.
        """
        self.latest_led = (mode, interval, r, g, b)

    def request_status(self):
        """
        Triggers a status request to obtain health feedback
        (battery voltage, motor temperature, fault code).
        """
        self.status_requested = True

    def get_position(self):
        """
        Returns the current position as (x, y, theta) in meters and radians.

        Returns:
            Tuple of (x, y, theta) in meters and radians
        """
        return self.position

    def get_battery_voltage(self):
        """
        Returns the current battery voltage in volts.

        Returns:
            Battery voltage in volts
        """
        return self.battery_voltage

    def get_motor_temperature(self):
        """
        Returns the current motor temperature in degrees Celsius.

        Returns:
            Motor temperature in degrees Celsius
        """
        return self.motor_temperature

    def get_fault_code(self):
        """
        Returns the current fault code.

        Returns:
            Fault code
        """
        return self.fault_code

    def __del__(self):
        """Clean up resources when the object is destroyed."""
        self.running = False
        time.sleep(0.1)  # Give the communication loop time to exit
        if hasattr(self, "ser") and self.ser:
            self.ser.close()
