#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Mars System Detection Script
============================
Quick check to verify all hardware components are detected by the Jetson.

Checks:
- Dynamixel servos 1-7 (arm + head)
- PCB communication (I2C)
- LiDAR (RPLidar)
- Cameras (USB cameras)
- Speaker (audio output)
"""

import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import me
import print_runtime_env

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DYNAMIXEL_DEVICE = "/dev/ttyACM0"
DYNAMIXEL_BAUDRATE = 1000000
SERVO_IDS = [1, 2, 3, 4, 5, 6, 7]  # All arm + head servos

LIDAR_DEVICE = "/dev/rplidar"  # Symlink typically created by udev rules
LIDAR_FALLBACK_PATTERN = "/dev/ttyUSB*"

I2C_BUS = 1
I2C_ADDRESS = 0x42
SPEAKER_SOUND = "/usr/share/sounds/sound-icons/electric-piano-3.wav"

TMUX_SESSION = "ros_nodes"
SYSTEMD_SERVICE = "ros-app.service"

SYSTEM_ENV_PATH = print_runtime_env.SYSTEM_ENV_PATH
# Fall back to this checkout rather than ~/innate-os: with the variable unset, guessing
# the home directory reads another checkout's env and reports on the wrong robot config.
INNATE_OS_ROOT = os.environ.get("INNATE_OS_ROOT") or str(Path(__file__).resolve().parent.parent)
ROBOT_INFO_PATH = os.path.join(INNATE_OS_ROOT, "data", "robot_info.json")
REPO_ENV_PATH = os.path.join(INNATE_OS_ROOT, ".env")

# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def ok(msg):
    print(f"  {Colors.GREEN}✓{Colors.END} {msg}")


def fail(msg):
    print(f"  {Colors.RED}✗{Colors.END} {msg}")


def warn(msg):
    print(f"  {Colors.YELLOW}⚠{Colors.END} {msg}")


def header(msg):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{msg}{Colors.END}")


def is_innate_service_running():
    r = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True)
    return r.returncode == 0


def stop_innate_service():
    """Stop the ROS nodes (systemd service and/or bare tmux session)."""
    r = subprocess.run(["systemctl", "is-active", "--quiet", SYSTEMD_SERVICE])
    if r.returncode == 0:
        # Inherit stdio so a sudo password prompt (if any) reaches the user
        if subprocess.run(["sudo", "systemctl", "stop", SYSTEMD_SERVICE]).returncode != 0:
            return False
    if is_innate_service_running():
        r = subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], capture_output=True)
        # Non-zero can also mean the session died on its own between the check
        # and the kill, so only fail if it is actually still alive
        if r.returncode != 0 and is_innate_service_running():
            return False

    for _ in range(20):
        if not is_innate_service_running():
            break
        time.sleep(0.5)
    else:
        return False

    # Give the dying nodes a moment to release the serial port and cameras
    time.sleep(2.0)
    return True


def ensure_services_stopped():
    """Offer to stop running ROS nodes — diagnostics needs exclusive hardware access.

    Returns True if the nodes were stopped here, False if they were not running.
    Exits if the user declines or the nodes cannot be stopped.
    """
    if not is_innate_service_running():
        return False

    warn("ROS nodes are running — diagnostics needs exclusive access to the")
    warn("hardware (serial port, cameras), so most checks would fail.")

    if not sys.stdin.isatty():
        fail("Stop the nodes first:  innate service stop")
        sys.exit(1)

    try:
        answer = input("\n  Stop all ROS nodes and run diagnostics? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

    if answer not in ("", "y", "yes"):
        fail("Aborted — stop the nodes first:  innate service stop")
        sys.exit(1)

    print("  Stopping ROS nodes...")
    if not stop_innate_service():
        fail("Failed to stop the ROS nodes — try manually:  innate service stop")
        sys.exit(1)

    ok("ROS nodes stopped")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# SERVO DETECTION
# ═══════════════════════════════════════════════════════════════════════════════


def check_servos():
    """Check if Dynamixel servos 1-7 are detected"""
    header("DYNAMIXEL SERVOS (1-7)")

    if is_innate_service_running():
        fail("Innate service is running and holding the serial port open")
        warn("Stop it first:  innate service stop")
        return False

    # Check if device exists
    if not os.path.exists(DYNAMIXEL_DEVICE):
        fail(f"Serial port not found: {DYNAMIXEL_DEVICE}")
        return False

    ok(f"Serial port found: {DYNAMIXEL_DEVICE}")

    try:
        from dynamixel_sdk import COMM_SUCCESS, PacketHandler, PortHandler
    except ImportError:
        fail("dynamixel_sdk not installed")
        return False

    port = PortHandler(DYNAMIXEL_DEVICE)
    packet = PacketHandler(2.0)

    try:
        if not port.openPort():
            fail("Failed to open serial port")
            return False
    except Exception as e:
        error_msg = str(e)
        error_type = type(e).__name__
        # Check for permission errors (can be SerialException, PermissionError, or OSError)
        if (
            "Permission denied" in error_msg
            or "PermissionError" in error_type
            or "[Errno 13]" in error_msg
            or "could not open port" in error_msg.lower()
        ):
            warn(f"Permission denied accessing {DYNAMIXEL_DEVICE}")
            warn("  User may need to be in 'dialout' group.")
            warn("  If post_update.sh added you to dialout, run: newgrp dialout")
            warn("  Or log out and back in for group changes to take effect")
            return False
        else:
            fail(f"Failed to open serial port: {error_msg}")
            return False

    if not port.setBaudRate(DYNAMIXEL_BAUDRATE):
        fail(f"Failed to set baudrate to {DYNAMIXEL_BAUDRATE}")
        port.closePort()
        return False

    ok(f"Serial port opened at {DYNAMIXEL_BAUDRATE} baud")

    detected = []
    missing = []

    for servo_id in SERVO_IDS:
        for attempt in range(5):
            try:
                _, result, _ = packet.ping(port, servo_id)
            except Exception as e:
                if attempt == 4:
                    fail(f"Serial error while pinging servo {servo_id}: {e}")
                    if is_innate_service_running():
                        warn("Innate service is running — stop it first:  innate service stop")
                    port.closePort()
                    return False
                time.sleep(0.2)
                continue
            if result == COMM_SUCCESS:
                detected.append(servo_id)
                break
            time.sleep(0.1)
        else:
            missing.append(servo_id)
        time.sleep(0.1)

    port.closePort()

    # Report results
    for sid in detected:
        ok(f"Servo {sid} detected")
    for sid in missing:
        fail(f"Servo {sid} NOT detected")

    return len(missing) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# LIDAR DETECTION
# ═══════════════════════════════════════════════════════════════════════════════


def check_lidar():
    """Check if RPLidar is detected"""
    header("LIDAR (RPLidar)")

    # Check primary device path
    if os.path.exists(LIDAR_DEVICE):
        ok(f"LiDAR found at {LIDAR_DEVICE}")
        # Check what it links to
        if os.path.islink(LIDAR_DEVICE):
            real_path = os.path.realpath(LIDAR_DEVICE)
            ok(f"  -> {real_path}")
        return True

    # Check fallback USB serial devices
    usb_devices = glob.glob(LIDAR_FALLBACK_PATTERN)
    if usb_devices:
        warn(f"Primary device {LIDAR_DEVICE} not found")
        warn(f"Found USB serial devices: {usb_devices}")
        warn("LiDAR may be on one of these devices")
        return True  # Partial success

    fail(f"LiDAR not found at {LIDAR_DEVICE}")
    fail("No USB serial devices found")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# CAMERA DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

# Expected camera identifiers
HEAD_CAMERA_ID = "3D_USB_Camera"  # Head camera identifier
ARM_CAMERA_ID = "Arducam"  # Arm camera identifier


def get_v4l2_cameras():
    """Get all V4L2 video capture devices with their info"""
    import fcntl
    import struct

    # V4L2 constants
    VIDIOC_QUERYCAP = 0x80685600
    V4L2_CAP_VIDEO_CAPTURE = 0x1

    cameras = []
    video_devices = sorted(glob.glob("/dev/video*"))

    for dev_path in video_devices:
        try:
            fd = os.open(dev_path, os.O_RDWR | os.O_NONBLOCK)
            try:
                # Query capabilities
                buf = bytearray(104)
                fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)

                # Parse driver and card name
                driver = buf[0:16].split(b"\x00")[0].decode("utf-8", errors="ignore")
                card = buf[16:48].split(b"\x00")[0].decode("utf-8", errors="ignore")
                bus_info = buf[48:80].split(b"\x00")[0].decode("utf-8", errors="ignore")

                # Check if it's a video capture device
                caps = struct.unpack("I", buf[84:88])[0]

                if caps & V4L2_CAP_VIDEO_CAPTURE:
                    cameras.append({"path": dev_path, "card": card, "driver": driver, "bus": bus_info})
            finally:
                os.close(fd)
        except OSError:
            pass

    return cameras


def check_cameras():
    """Check if both cameras (head and arm) are detected"""
    header("CAMERAS")

    # Also check symlinks in /dev/v4l/by-id/
    symlink_dir = "/dev/v4l/by-id"
    symlinks = []
    if os.path.exists(symlink_dir):
        symlinks = os.listdir(symlink_dir)

    # Get all V4L2 cameras
    cameras = get_v4l2_cameras()

    if not cameras:
        fail("No video capture devices found")
        return False

    # Look for head camera (3D USB Camera)
    head_camera = None
    arm_camera = None

    for cam in cameras:
        card_lower = cam["card"].lower()

        # Check for head camera
        if "3d" in card_lower and "usb" in card_lower and "camera" in card_lower:
            head_camera = cam
        # Check for arm camera
        elif "arducam" in card_lower:
            arm_camera = cam

    # Also check by symlinks if not found by card name
    if not head_camera:
        for link in symlinks:
            if HEAD_CAMERA_ID in link:
                # Found by symlink, try to find matching camera
                link_path = os.path.join(symlink_dir, link)
                real_path = os.path.realpath(link_path)
                for cam in cameras:
                    if cam["path"] == real_path:
                        head_camera = cam
                        break
                if not head_camera:
                    head_camera = {"path": real_path, "card": link, "driver": "via symlink", "bus": ""}

    # Report results
    if head_camera:
        ok(f"Head camera: {head_camera['path']} ({head_camera['card']})")
    else:
        fail(f"Head camera NOT found (looking for '{HEAD_CAMERA_ID}')")

    if arm_camera:
        ok(f"Arm camera: {arm_camera['path']} ({arm_camera['card']})")
    else:
        fail(f"Arm camera NOT found (looking for '{ARM_CAMERA_ID}')")

    # Both cameras must be present
    if head_camera and arm_camera:
        ok("Both cameras detected")
        return True
    elif head_camera:
        warn("Only head camera detected, arm camera missing")
        return False
    elif arm_camera:
        warn("Only arm camera detected, head camera missing")
        return False
    else:
        fail("No expected cameras detected")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PCB DETECTION (I2C)
# ═══════════════════════════════════════════════════════════════════════════════


def check_pcb():
    """Check if PCB responds to status request via I2C"""
    header("PCB (I2C)")

    try:
        import smbus2 as smbus
    except ImportError:
        try:
            import smbus
        except ImportError:
            fail("smbus library not installed")
            return False

    try:
        bus = smbus.SMBus(I2C_BUS)
    except Exception as e:
        fail(f"Failed to open I2C bus {I2C_BUS}: {e}")
        return False

    # Build status request (CMD_STATUS=0x03 + 6 zero bytes + CRC)
    message = [0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    crc = 0x00
    for byte in message:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 0x01 else crc >> 1
    message.append(crc)

    try:
        bus.write_i2c_block_data(I2C_ADDRESS, 0x00, message)
        time.sleep(0.01)
        response = bus.read_i2c_block_data(I2C_ADDRESS, 0x00, 8)
        bus.close()

        if response[0] == 0x83:  # RESP_STATUS
            ok(f"PCB responded on I2C bus {I2C_BUS} address 0x{I2C_ADDRESS:02X}")
            return True
        else:
            fail(f"Unexpected response: {response[0]:#x}")
            return False
    except Exception as e:
        bus.close()
        fail(f"PCB not responding: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# SPEAKER DETECTION
# ═══════════════════════════════════════════════════════════════════════════════


def check_speaker():
    """Play a test sound (requires human verification)"""
    header("SPEAKER")

    import subprocess

    if not os.path.exists(SPEAKER_SOUND):
        fail(f"Test sound not found: {SPEAKER_SOUND}")
        return False

    try:
        result = subprocess.run(["aplay", SPEAKER_SOUND], capture_output=True, timeout=10)
        if result.returncode == 0:
            warn("Sound played - verify you heard it (no automatic confirmation possible)")
            return True
        else:
            fail(f"aplay failed: {result.stderr.decode()}")
            return False
    except FileNotFoundError:
        fail("aplay not found")
        return False
    except Exception as e:
        fail(f"Speaker test failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# IDENTITY
# ═══════════════════════════════════════════════════════════════════════════════


def _local_identity():
    """(robot_id, robot_id from robot_info.json, service key) as the nodes see them.

    The merge — /etc/innate.env as fallback, repo .env on top — belongs to
    print_runtime_env.py, which is what the ROS processes are actually launched with.
    """
    env = print_runtime_env.build_runtime_env(Path(INNATE_OS_ROOT))

    robot_info_id = None
    try:
        with open(ROBOT_INFO_PATH) as f:
            robot_info_id = json.load(f).get("robot_id")
    except (OSError, ValueError):
        pass

    return env.get("ROBOT_ID"), robot_info_id, env.get("INNATE_SERVICE_KEY", "").strip()


def check_identity():
    """Compare the robot's stored identity against what its service key resolves to.

    Provisioning writes the identity as one blob composed on a laptop, so the robot
    never proves key↔identity agreement itself. This is the only on-robot detector of a
    mis-composed blob — loud, never fatal.
    """
    header("IDENTITY")

    env_id, info_id, service_key = _local_identity()

    if not service_key:
        # An unreadable file parses as an empty one, so a provisioned robot whose
        # /etc/innate.env lost its group would otherwise report a clean "Unprovisioned"
        # — the same state that silently drops the key out of the runtime env.
        if SYSTEM_ENV_PATH.exists() and not os.access(SYSTEM_ENV_PATH, os.R_OK):
            fail(f"{SYSTEM_ENV_PATH} exists but this account cannot read it — the nodes lose the key too")
            warn(f"  sudo chown root:$(id -gn) {SYSTEM_ENV_PATH} && sudo chmod 640 {SYSTEM_ENV_PATH}")
            return False
        warn(
            f"Unprovisioned: no INNATE_SERVICE_KEY in {SYSTEM_ENV_PATH} or the repo .env (robot_id {env_id or 'unset'})"
        )
        return True

    ok(f"Service key present, robot_id {env_id or 'unset'}")

    if info_id and env_id and info_id != env_id:
        fail(f"robot_info.json says {info_id}, the merged env says {env_id}")
        warn(f"  ROBOT_ID is read from {SYSTEM_ENV_PATH} with {REPO_ENV_PATH} layered on top — check both")
        warn(f"  Delete robot_info.json and restart ros-app, or restore {SYSTEM_ENV_PATH}.bak")

    try:
        cloud = me.fetch_identity(service_key)
    except urllib.error.HTTPError as e:
        # The service answered and said no — a revoked or mistyped key, not a network.
        fail(f"The auth service rejected this key (HTTP {e.code})")
        return False
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        warn(f"Offline, using cached identity (could not reach auth service: {e})")
        return True

    cloud_id = me.robot_id(cloud)
    if not cloud_id:
        warn(f"Auth service returned no robot id (keys: {sorted(cloud)})")
        return True

    if env_id != cloud_id:
        fail(f"This key belongs to {cloud_id}, but this robot calls itself {env_id or 'unset'}")
        warn(f"  Re-provision: sudo rm {SYSTEM_ENV_PATH}, then provision.py from the laptop")
        return False

    ok(f"Key and stored identity agree: {cloud_id}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    print(f"\n{Colors.BOLD}{Colors.CYAN}╔══════════════════════════════════════════════════════════╗")
    print("║              MARS SYSTEM CHECK                           ║")
    print(f"╚══════════════════════════════════════════════════════════╝{Colors.END}")

    nodes_stopped = ensure_services_stopped()

    results = {}

    # Wrap check_servos to catch any unhandled exceptions from SDK
    try:
        results["servos"] = check_servos()
    except Exception as e:
        # If exception wasn't caught in check_servos(), handle it here
        error_msg = str(e)
        if "[Errno 13]" in error_msg or "Permission denied" in error_msg:
            header("DYNAMIXEL SERVOS (1-7)")
            ok(f"Serial port found: {DYNAMIXEL_DEVICE}")
            warn(f"Permission denied accessing {DYNAMIXEL_DEVICE}")
            warn("  User may need to be in 'dialout' group.")
            warn("  If post_update.sh added you to dialout, run: newgrp dialout")
            warn("  Or log out and back in for group changes to take effect")
            results["servos"] = False
        else:
            # Re-raise if it's a different error
            raise

    results["pcb"] = check_pcb()
    results["lidar"] = check_lidar()
    results["cameras"] = check_cameras()
    results["speaker"] = check_speaker()
    results["identity"] = check_identity()

    # Summary
    header("SUMMARY")
    all_ok = True

    for name, passed in results.items():
        if name == "speaker":
            if passed:
                warn("SPEAKER: Verify sound was heard")
            else:
                fail("SPEAKER: FAILED")
                all_ok = False
        elif passed:
            ok(f"{name.upper()}: OK")
        else:
            fail(f"{name.upper()}: FAILED")
            all_ok = False

    if all_ok:
        print(f"\n{Colors.GREEN}{Colors.BOLD}All systems detected! ✓{Colors.END}\n")
    else:
        print(f"\n{Colors.RED}{Colors.BOLD}Some components missing.{Colors.END}\n")

    if nodes_stopped:
        warn("ROS nodes were stopped for diagnostics — restart them with:  innate service start")
        print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
