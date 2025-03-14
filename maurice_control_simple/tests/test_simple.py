#!/usr/bin/env python3
"""
Simple integrated test for Maurice robot controllers.

This test verifies that both controllers send the correct signals when called.
"""

import sys
import unittest
import struct
from unittest.mock import patch, MagicMock

# Mock the dynamixel_sdk module before importing the controllers
sys.modules["dynamixel_sdk"] = MagicMock()

# Now import the controllers
from maurice_control_simple.dynamixel import Dynamixel, OperatingMode
from maurice_control_simple.wheel_controller import WheelController


class TestControllers(unittest.TestCase):
    """Simple integrated tests for both controllers."""

    def test_dynamixel_sends_correct_commands(self):
        """Test that Dynamixel controller sends correct commands."""
        # Mock the Dynamixel SDK
        with patch(
            "maurice_control_simple.dynamixel.PortHandler"
        ) as mock_port_handler_class, patch(
            "maurice_control_simple.dynamixel.PacketHandler"
        ) as mock_packet_handler_class:

            # Configure mocks
            mock_port_handler = mock_port_handler_class.return_value
            mock_packet_handler = mock_packet_handler_class.return_value
            mock_port_handler.openPort.return_value = True
            mock_port_handler.setBaudRate.return_value = True

            # Create controller with parameters split across lines
            controller = Dynamixel(
                baudrate=1000000, device_name="/dev/ttyACM0", protocol_version=2.0
            )

            # Test position control
            motor_id = 1
            position = 2000

            # Set mode and position
            controller.set_operating_mode(motor_id, OperatingMode.POSITION)
            controller._enable_torque(motor_id)
            controller.set_position(motor_id, position)

            # Verify correct commands were sent
            mock_packet_handler.write1ByteTxRx.assert_any_call(
                mock_port_handler,
                motor_id,
                controller.OPERATING_MODE_ADDR,
                OperatingMode.POSITION.value,
            )
            mock_packet_handler.write1ByteTxRx.assert_any_call(
                mock_port_handler, motor_id, controller.ADDR_TORQUE_ENABLE, 1
            )
            # Split long line into multiple lines
            mock_packet_handler.write4ByteTxRx.assert_called_with(
                mock_port_handler, motor_id, controller.ADDR_GOAL_POSITION, position
            )

    def test_wheel_controller_sends_correct_commands(self):
        """Test that WheelController sends correct commands."""
        # Mock the serial interface
        with patch(
            "maurice_control_simple.wheel_controller.serial.Serial"
        ) as mock_serial_class, patch(
            "maurice_control_simple.wheel_controller.threading.Thread"
        ), patch(
            "maurice_control_simple.wheel_controller.time.sleep"
        ):

            # Configure mocks
            mock_serial = mock_serial_class.return_value
            mock_serial.in_waiting = 0

            # Create controller
            controller = WheelController(
                port="/dev/ttyTHS1", baud_rate=115200, debug=False
            )

            # Reset mock to clear initialization calls
            mock_serial.reset_mock()

            # Test speed command
            v = 0.5  # 0.5 m/s
            omega = 0.2  # 0.2 rad/s

            # Set speed and manually trigger the command
            controller.set_speed(v, omega)
            controller._send_speed_command()

            # Verify correct command was sent
            self.assertEqual(mock_serial.write.call_count, 1)

            # Calculate expected payload
            speed_int = int(v * 100)  # 50
            turn_int = int(omega * 100)  # 20
            expected_payload = struct.pack(">hhH", speed_int, turn_int, 0x0000)

            # Calculate expected packet
            protocol_msg = bytes([controller.CMD_MOVE]) + expected_payload
            crc = controller._calculate_crc(protocol_msg)
            expected_packet = controller.SOM_MARKER + protocol_msg + bytes([crc])

            # Verify the correct data was sent
            args = mock_serial.write.call_args[0]
            self.assertEqual(args[0], expected_packet)


if __name__ == "__main__":
    unittest.main()
