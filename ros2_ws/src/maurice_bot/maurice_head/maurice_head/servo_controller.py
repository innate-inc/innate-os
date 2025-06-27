import serial
import time
import struct



class ServoController:
    def __init__(self, port="/dev/ttyACM0", baudrate=115200, timeout=0.2):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.TIMEOUT_BETW_BYTES = 0.2
        self.MIN_RAW_VALUE = 1
        self.MAX_RAW_VALUE = 4096
        self.MIN_DEGREE = 0.0
        self.MAX_DEGREE = 360.0

    def deg2RawValue(self, degrees):
        """
        Convert degrees to raw servo value.
        Args:
            degrees (float): Angle in degrees between 0 and 360
        Returns:
            int: Raw servo value between 1 and 4096
        Raises:
            ValueError: If degrees is outside the valid range
        """
        if not self.MIN_DEGREE <= degrees <= self.MAX_DEGREE:
            raise ValueError(f"Degree value must be between {self.MIN_DEGREE} and {self.MAX_DEGREE}")

        # Linear mapping from [0, 360] to [1, 4096]
        raw_value = int(round((degrees - self.MIN_DEGREE) * (self.MAX_RAW_VALUE - self.MIN_RAW_VALUE) /
                            (self.MAX_DEGREE - self.MIN_DEGREE) + self.MIN_RAW_VALUE))

        return raw_value

    def rawValue2Deg(self, raw_value):
        """
        Convert raw servo value to degrees.
        Args:
            raw_value (int): Raw servo value between 1 and 4096
        Returns:
            float: Angle in degrees between 0 and 360
        Raises:
            ValueError: If raw_value is outside the valid range
        """
        if not self.MIN_RAW_VALUE <= raw_value <= self.MAX_RAW_VALUE:
            raise ValueError(f"Raw value must be between {self.MIN_RAW_VALUE} and {self.MAX_RAW_VALUE}")

        # Linear mapping from [1, 4096] to [0, 360]
        degrees = (raw_value - self.MIN_RAW_VALUE) * (self.MAX_DEGREE - self.MIN_DEGREE) / \
                 (self.MAX_RAW_VALUE - self.MIN_RAW_VALUE) + self.MIN_DEGREE

        return round(degrees, 2)  # Round to 2 decimal places for practical use

    def open_connection(self):
        try:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except serial.SerialException as e:
            print(f"Error opening serial port: {e}")

    def close_connection(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def create_message(self, message_type, servo_id, data):
        data_length = len(data)
        payload = struct.pack(f'{data_length}B', *data)
        message = [message_type, servo_id, data_length] + list(payload)
        return bytearray(message)

    def read_fixed_response(self):
        """Read exactly 5 bytes from the serial port."""
        try:
            response = self.ser.read(5)
            return response.decode(errors='ignore')
        except Exception as e:
            print(f"Error reading response: {e}")
            return ""

    def set_position(self, servo_id, degrees):
        """
        Set servo position using degrees.
        Args:
            servo_id (int): ID of the servo
            degrees (float): Target position in degrees (0 to 360)
        """
        raw_position = self.deg2RawValue(degrees)
        message_type = 0x01
        data = [raw_position >> 8, raw_position & 0xFF]
        message = self.create_message(message_type, servo_id, data)
        try:
            self.open_connection()
            self.ser.write(message)
            # response = self.read_fixed_response()
            # print(f"Position Response: {response}")
        finally:
            self.close_connection()

    def read_position(self, servo_id):
        """
        Read current servo position in degrees.
        Args:
            servo_id (int): ID of the servo
        Returns:
            float: Current position in degrees (0 to 360) or None if read fails
        """
        message_type = 0x02
        data = [0, 0]
        message = self.create_message(message_type, servo_id, data)

        try:
            self.open_connection()
            self.ser.write(message)
            response = self.ser.read(4)
            if len(response) == 4:
                raw_position = struct.unpack('>I', response)[0]
                return self.rawValue2Deg(raw_position)
        finally:
            self.close_connection()
        return None

    def set_torque(self, servo_id, activate):
        message_type = 0x03 if activate else 0x04
        data = [0, 0]
        message = self.create_message(message_type, servo_id, data)
        try:
            self.open_connection()
            self.ser.write(message)
            response = self.read_fixed_response()
            print(f"Torque Response: {response}")
        finally:
            self.close_connection()
