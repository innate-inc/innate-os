#!/usr/bin/env /Users/theomichel/Robots/Innate/innate-os/.venv/bin/python3
"""
Leader Arm UDP Broadcaster

Reads leader arm positions and broadcasts UDP packets to all robots.
Uses broadcast address so all robots on the network receive commands simultaneously.

Packet format (38 bytes, little-endian):
  - Bytes 0-1:   Magic header (0xAA55)
  - Bytes 2-5:   Sequence number (uint32)
  - Bytes 6-13:  Timestamp (double, ms since epoch)
  - Bytes 14-37: 6 servo positions (int32 each)
"""

import socket
import struct
import time
import argparse
import sys

# Add maurice_control to path
sys.path.insert(
    0,
    "/Users/theomichel/Robots/Innate/innate-os/ros2_ws/src/maurice_bot/maurice_control",
)

from maurice_control.dynamixel import Dynamixel
from maurice_control.robot import Robot

MAGIC_HEADER = 0xAA55
RESET_MAGIC = 0xAA56
PORT = 9999


def create_packet(sequence: int, positions: list[int]) -> bytes:
    """Create a 38-byte UDP packet."""
    timestamp = time.time() * 1000  # ms since epoch
    return struct.pack("<HId6i", MAGIC_HEADER, sequence, timestamp, *positions)


def create_reset_packet(sequence: int) -> bytes:
    """Create a 6-byte reset packet."""
    return struct.pack("<HI", RESET_MAGIC, sequence)


def main():
    parser = argparse.ArgumentParser(
        description="Broadcast leader arm positions via UDP"
    )
    parser.add_argument("--device", default="/dev/ttyACM1", help="Serial device")
    parser.add_argument("--baud", type=int, default=1000000, help="Baud rate")
    parser.add_argument("--hz", type=float, default=100.0, help="Control frequency")
    parser.add_argument("--port", type=int, default=PORT, help="UDP port")
    parser.add_argument(
        "--broadcast", default="255.255.255.255", help="Broadcast address"
    )
    args = parser.parse_args()

    # Initialize leader arm
    print(f"Connecting to leader arm on {args.device}...")
    dynamixel = Dynamixel.Config(
        baudrate=args.baud, device_name=args.device
    ).instantiate()
    robot = Robot(dynamixel=dynamixel, servo_ids=[1, 2, 3, 4, 5, 6])

    # Create UDP socket with broadcast enabled
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    # Send reset packet
    sock.sendto(create_reset_packet(0), (args.broadcast, args.port))
    print(f"Broadcasting to {args.broadcast}:{args.port} at {args.hz}Hz")

    sequence = 0
    period = 1.0 / args.hz

    try:
        while True:
            t0 = time.perf_counter()
            positions = robot.read_position()
            packet = create_packet(sequence, positions)
            sock.sendto(packet, (args.broadcast, args.port))
            sequence += 1

            elapsed = time.perf_counter() - t0
            if elapsed < period:
                time.sleep(period - elapsed)
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        sock.close()
        dynamixel.disconnect()


if __name__ == "__main__":
    main()
