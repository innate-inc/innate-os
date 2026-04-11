"""
Leader Arm UDP Sender

Reads servo positions from a Dynamixel leader arm connected locally (e.g. /dev/ttyACM0)
and streams them via UDP to the robot's udp_leader_receiver node.

Usage:
    python3 leader_arm_sender.py --robot-ip <JETSON_IP> [options]

Requirements:
    pip install dynamixel-sdk

Packet format (38 bytes, little-endian) — matches udp_leader_receiver.cpp:
    Bytes 0-1:   Magic header (0xAA55)
    Bytes 2-5:   Sequence number (uint32)
    Bytes 6-13:  Timestamp (double, ms since epoch)
    Bytes 14-37: Servo positions 1-6 (int32 each)
"""

import argparse
import socket
import struct
import sys
import time

MAGIC_HEADER = 0xAA55
RESET_MAGIC_HEADER = 0xAA56
PACKET_FORMAT = "<HId6i"  # magic(u16), seq(u32), timestamp(f64), 6x pos(i32)
PACKET_SIZE = 38
RESET_FORMAT = "<HI"  # magic(u16), seq(u32)

DEFAULT_PORT = 9999
DEFAULT_DEVICE = "/dev/ttyACM0"
DEFAULT_BAUD = 1000000
DEFAULT_SERVO_IDS = [1, 2, 3, 4, 5, 6]
DEFAULT_FREQ = 100.0  # Hz


def build_packet(sequence: int, positions: list[int]) -> bytes:
    timestamp_ms = time.time() * 1000.0
    return struct.pack(PACKET_FORMAT, MAGIC_HEADER, sequence, timestamp_ms, *positions)


def build_reset_packet(sequence: int) -> bytes:
    return struct.pack(RESET_FORMAT, RESET_MAGIC_HEADER, sequence)


def main():
    parser = argparse.ArgumentParser(description="Stream leader arm positions to the robot via UDP")
    parser.add_argument("--robot-ip", required=True, help="IP address of the robot (Jetson)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"UDP port (default: {DEFAULT_PORT})")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"Serial device (default: {DEFAULT_DEVICE})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--servo-ids", type=int, nargs="+", default=DEFAULT_SERVO_IDS,
                        help=f"Servo IDs (default: {DEFAULT_SERVO_IDS})")
    parser.add_argument("--freq", type=float, default=DEFAULT_FREQ,
                        help=f"Send frequency in Hz (default: {DEFAULT_FREQ})")
    args = parser.parse_args()

    try:
        from dynamixel_sdk import PacketHandler, PortHandler, COMM_SUCCESS
    except ImportError:
        print("ERROR: dynamixel_sdk not found. Install it with: pip install dynamixel-sdk")
        sys.exit(1)

    print(f"Connecting to leader arm on {args.device} at {args.baud} baud...")
    port_handler = PortHandler(args.device)
    packet_handler = PacketHandler(2.0)  # Protocol 2.0

    if not port_handler.openPort():
        print(f"ERROR: Failed to open port {args.device}")
        sys.exit(1)
    if not port_handler.setBaudRate(args.baud):
        print(f"ERROR: Failed to set baud rate {args.baud}")
        port_handler.closePort()
        sys.exit(1)

    print(f"Connected. Streaming to {args.robot_ip}:{args.port} at {args.freq} Hz")
    print("Press Ctrl+C to stop.\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.robot_ip, args.port)

    # Send a reset packet so the receiver clears any stale sequence state
    sock.sendto(build_reset_packet(0), target)

    sequence = 1
    period = 1.0 / args.freq
    sent = 0
    errors = 0
    t_start = time.time()

    ADDR_PRESENT_POSITION = 132

    def read_position(servo_id: int) -> int | None:
        pos, result, _ = packet_handler.read4ByteTxRx(port_handler, servo_id, ADDR_PRESENT_POSITION)
        if result != COMM_SUCCESS:
            return None
        # Handle signed 32-bit: Dynamixel returns unsigned; values > 2^31 are negative
        if pos > 2**31:
            pos -= 2**32
        return pos

    try:
        while True:
            t_loop = time.time()

            positions = []
            read_ok = True
            for sid in args.servo_ids:
                pos = read_position(sid)
                if pos is None:
                    errors += 1
                    read_ok = False
                    break
                positions.append(pos)

            if read_ok:
                packet = build_packet(sequence, positions)
                sock.sendto(packet, target)
                sequence += 1
                sent += 1

                if sent % 100 == 0:
                    elapsed = time.time() - t_start
                    actual_hz = sent / elapsed
                    print(f"  sent={sent}  errors={errors}  actual={actual_hz:.1f}Hz  "
                          f"pos={positions}")

            # Sleep for the remainder of the period
            elapsed = time.time() - t_loop
            sleep_time = period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print(f"\nStopped. Sent {sent} packets, {errors} read errors.")
    finally:
        port_handler.closePort()
        sock.close()


if __name__ == "__main__":
    main()
