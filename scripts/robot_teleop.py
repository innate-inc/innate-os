#!/usr/bin/env python3
"""
Simple UDP Teleoperation Interface for MARS Robot Arms

Usage:
    # Control a single robot
    controller = RobotTeleop("192.168.1.101")
    controller.send_positions([2048, 2048, 2048, 2048, 2048, 2048])
    
    # Control multiple robots at once
    controller = RobotTeleop(["192.168.1.101", "192.168.1.102", "192.168.1.103"])
    controller.send_positions([2048, 2048, 2048, 2048, 2048, 2048])

Packet Format (38 bytes, little-endian):
    - Bytes 0-1:   Magic header (0xAA55)
    - Bytes 2-5:   Sequence number (uint32)
    - Bytes 6-13:  Timestamp (double, ms since epoch)
    - Bytes 14-37: 6 servo positions (int32 each)

Servo positions: 0-4095 (center = 2048, maps to -π to +π radians)
"""

import socket
import struct
import time
from typing import List, Union
import math


class RobotTeleop:
    """UDP teleoperation controller for MARS robot arms."""
    
    MAGIC_HEADER = 0xAA55
    RESET_MAGIC_HEADER = 0xAA56
    DEFAULT_PORT = 9999
    
    # Servo position constants
    CENTER_POSITION = 2048
    MIN_POSITION = 0
    MAX_POSITION = 4095
    
    def __init__(self, robot_ips: Union[str, List[str]], port: int = DEFAULT_PORT):
        """
        Initialize the teleoperation controller.
        
        Args:
            robot_ips: Single IP string or list of IP addresses
            port: UDP port (default 9999)
        """
        if isinstance(robot_ips, str):
            self.robot_ips = [robot_ips]
        else:
            self.robot_ips = list(robot_ips)
        
        self.port = port
        self.sequence = 0
        
        # Create UDP socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        print(f"RobotTeleop initialized for {len(self.robot_ips)} robot(s) on port {port}")
    
    def send_positions(self, positions: List[int]) -> None:
        """
        Send servo positions to all robots.
        
        Args:
            positions: List of 6 servo positions (0-4095, center=2048)
        """
        if len(positions) != 6:
            raise ValueError(f"Expected 6 positions, got {len(positions)}")
        
        # Clamp positions to valid range
        positions = [max(self.MIN_POSITION, min(self.MAX_POSITION, int(p))) for p in positions]
        
        # Build packet
        timestamp = time.time() * 1000  # ms since epoch
        packet = struct.pack(
            '<HId6i',  # little-endian: ushort, uint, double, 6 ints
            self.MAGIC_HEADER,
            self.sequence,
            timestamp,
            *positions
        )
        
        # Send to all robots
        for ip in self.robot_ips:
            self.socket.sendto(packet, (ip, self.port))
        
        self.sequence += 1
    
    def send_radians(self, radians: List[float]) -> None:
        """
        Send servo positions in radians (more intuitive).
        
        Args:
            radians: List of 6 joint angles in radians (-π to +π)
        """
        if len(radians) != 6:
            raise ValueError(f"Expected 6 angles, got {len(radians)}")
        
        # Convert radians to servo positions: position = radians * (4096 / 2π) + 2048
        positions = [int(r * (4096 / (2 * math.pi)) + 2048) for r in radians]
        self.send_positions(positions)
    
    def send_degrees(self, degrees: List[float]) -> None:
        """
        Send servo positions in degrees.
        
        Args:
            degrees: List of 6 joint angles in degrees (-180 to +180)
        """
        radians = [math.radians(d) for d in degrees]
        self.send_radians(radians)
    
    def reset_sequence(self) -> None:
        """
        Send reset packet to reset sequence tracking on robots.
        Useful when starting a new teleoperation session.
        """
        packet = struct.pack(
            '<HI',  # little-endian: ushort, uint
            self.RESET_MAGIC_HEADER,
            self.sequence
        )
        
        for ip in self.robot_ips:
            self.socket.sendto(packet, (ip, self.port))
        
        self.sequence = 0
        print("Sequence reset sent to all robots")
    
    def home(self) -> None:
        """Send all servos to center/home position."""
        self.send_positions([self.CENTER_POSITION] * 6)
        print("Sent home position to all robots")
    
    def add_robot(self, ip: str) -> None:
        """Add a robot IP to the control list."""
        if ip not in self.robot_ips:
            self.robot_ips.append(ip)
            print(f"Added robot: {ip}")
    
    def remove_robot(self, ip: str) -> None:
        """Remove a robot IP from the control list."""
        if ip in self.robot_ips:
            self.robot_ips.remove(ip)
            print(f"Removed robot: {ip}")
    
    def close(self) -> None:
        """Close the UDP socket."""
        self.socket.close()
        print("Socket closed")


def demo_sine_wave(controller: RobotTeleop, duration: float = 10.0, frequency: float = 0.5):
    """
    Demo: Move servos in a sine wave pattern.
    
    Args:
        controller: RobotTeleop instance
        duration: How long to run (seconds)
        frequency: Wave frequency (Hz)
    """
    print(f"Running sine wave demo for {duration}s...")
    start_time = time.time()
    
    try:
        while time.time() - start_time < duration:
            t = time.time() - start_time
            
            # Create sine wave for each joint with phase offset
            angles = [
                30 * math.sin(2 * math.pi * frequency * t + i * 0.5)
                for i in range(6)
            ]
            
            controller.send_degrees(angles)
            time.sleep(0.01)  # 100Hz update rate
            
    except KeyboardInterrupt:
        print("\nDemo interrupted")
    
    # Return to home
    controller.home()
    print("Demo complete")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="MARS Robot UDP Teleoperation")
    parser.add_argument("--ip", "-i", type=str, nargs="+", required=True,
                        help="Robot IP address(es)")
    parser.add_argument("--port", "-p", type=int, default=9999,
                        help="UDP port (default: 9999)")
    parser.add_argument("--demo", action="store_true",
                        help="Run sine wave demo")
    parser.add_argument("--home", action="store_true",
                        help="Send home position")
    parser.add_argument("--reset", action="store_true",
                        help="Reset sequence counter")
    parser.add_argument("--positions", type=int, nargs=6, metavar="POS",
                        help="Send specific positions (6 values, 0-4095)")
    parser.add_argument("--degrees", type=float, nargs=6, metavar="DEG",
                        help="Send specific angles in degrees (6 values)")
    
    args = parser.parse_args()
    
    # Create controller
    controller = RobotTeleop(args.ip, args.port)
    
    try:
        if args.reset:
            controller.reset_sequence()
        
        if args.home:
            controller.home()
        elif args.positions:
            controller.send_positions(args.positions)
            print(f"Sent positions: {args.positions}")
        elif args.degrees:
            controller.send_degrees(args.degrees)
            print(f"Sent degrees: {args.degrees}")
        elif args.demo:
            demo_sine_wave(controller)
        else:
            # Interactive mode
            print("\nInteractive mode - enter commands:")
            print("  home          - send home position")
            print("  reset         - reset sequence counter")
            print("  pos x x x x x x - send positions (0-4095)")
            print("  deg x x x x x x - send degrees (-180 to 180)")
            print("  demo          - run sine wave demo")
            print("  quit          - exit")
            print()
            
            while True:
                try:
                    cmd = input("> ").strip().lower().split()
                    if not cmd:
                        continue
                    
                    if cmd[0] == "quit" or cmd[0] == "q":
                        break
                    elif cmd[0] == "home":
                        controller.home()
                    elif cmd[0] == "reset":
                        controller.reset_sequence()
                    elif cmd[0] == "demo":
                        demo_sine_wave(controller)
                    elif cmd[0] == "pos" and len(cmd) == 7:
                        positions = [int(x) for x in cmd[1:7]]
                        controller.send_positions(positions)
                        print(f"Sent: {positions}")
                    elif cmd[0] == "deg" and len(cmd) == 7:
                        degrees = [float(x) for x in cmd[1:7]]
                        controller.send_degrees(degrees)
                        print(f"Sent: {degrees}°")
                    else:
                        print("Unknown command. Type 'quit' to exit.")
                        
                except ValueError as e:
                    print(f"Invalid input: {e}")
                except KeyboardInterrupt:
                    print()
                    break
                    
    finally:
        controller.close()




