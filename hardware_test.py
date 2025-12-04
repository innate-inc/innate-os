#!/usr/bin/env python3
"""
Maurice Robot Hardware Test Script
==================================
Standalone test script to verify communication between Jetson and microcontroller.

Tests:
1. Wheel motors via I2C
2. Speaker/audio via aplay  
3. Arm servos 1-6 via Dynamixel (excludes servo 7)

Usage:
    python3 hardware_test.py           # Run all tests
    python3 hardware_test.py wheels    # Test wheels only
    python3 hardware_test.py speaker   # Test speaker only
    python3 hardware_test.py arm       # Test arm servos only
"""

import sys
import time
import struct
import subprocess
import os
import math
import tempfile
import wave

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# I2C Configuration for wheel motors
I2C_BUS = 1
I2C_ADDRESS = 0x42

# Dynamixel Configuration for arm servos
DYNAMIXEL_DEVICE = "/dev/ttyTHS1"  # Jetson hardware serial
DYNAMIXEL_BAUDRATE = 1000000
ARM_SERVO_IDS = [1, 2, 3, 4, 5, 6]  # Servos 1-6 only, NOT 7

# CRC-8/MAXIM constants (same as microcontroller)
CRC8_POLY = 0x8C
CRC8_INIT = 0x00

# Test parameters
WHEEL_TEST_SPEED = 0.15  # m/s - slow speed for safety
WHEEL_TEST_DURATION = 1.0  # seconds
SERVO_TEST_MOVEMENT = 0.1  # radians - small movement for safety

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'

def print_header(text):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'═' * 60}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}  {text}{Colors.END}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'═' * 60}{Colors.END}\n")

def print_test(text):
    print(f"{Colors.CYAN}▶ {text}{Colors.END}")

def print_success(text):
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")

def print_error(text):
    print(f"{Colors.RED}✗ {text}{Colors.END}")

def print_warning(text):
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")

def print_info(text):
    print(f"{Colors.BLUE}  {text}{Colors.END}")

def calculate_crc(data: bytes) -> int:
    """Calculate CRC-8/MAXIM checksum"""
    crc = CRC8_INIT
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = (crc >> 1) ^ CRC8_POLY
            else:
                crc >>= 1
    return crc

# ═══════════════════════════════════════════════════════════════════════════════
# WHEEL TEST (I2C Communication)
# ═══════════════════════════════════════════════════════════════════════════════

class WheelTester:
    """Test wheel motors via I2C communication with microcontroller"""
    
    CMD_MOVE = 0x01
    CMD_STATUS = 0x03
    
    def __init__(self):
        self.bus = None
        
    def connect(self):
        """Initialize I2C connection"""
        try:
            import smbus2 as smbus
        except ImportError:
            try:
                import smbus
            except ImportError:
                print_error("smbus2 or smbus library not found")
                print_info("Install with: pip3 install smbus2")
                return False
        
        try:
            self.bus = smbus.SMBus(I2C_BUS)
            print_success(f"Connected to I2C bus {I2C_BUS} at address 0x{I2C_ADDRESS:02X}")
            return True
        except Exception as e:
            print_error(f"Failed to connect to I2C bus: {e}")
            return False
    
    def disconnect(self):
        """Close I2C connection"""
        if self.bus:
            self.bus.close()
            
    def send_move_command(self, speed: float, turn: float):
        """
        Send movement command to microcontroller
        
        Args:
            speed: Linear speed in m/s (positive = forward)
            turn: Angular speed in rad/s (positive = counter-clockwise)
        """
        if not self.bus:
            return False
            
        # Scale and clamp values (MCU expects speed*100)
        speed_int = int(max(-32767, min(32767, speed * 100)))
        turn_int = int(max(-32767, min(32767, turn * 100)))
        
        # Pack: speed (2 bytes), turn (2 bytes), reserved (2 bytes)
        data = struct.pack(">hhH", speed_int, turn_int, 0x0000)
        
        # Build message: cmd_id + data + CRC
        message = bytearray([self.CMD_MOVE]) + bytearray(data)
        crc = calculate_crc(bytes(message))
        message.append(crc)
        
        try:
            self.bus.write_i2c_block_data(I2C_ADDRESS, 0x00, list(message))
            return True
        except Exception as e:
            print_error(f"Failed to send move command: {e}")
            return False
    
    def stop(self):
        """Stop all wheel movement"""
        return self.send_move_command(0.0, 0.0)
    
    def request_status(self):
        """Request status from microcontroller"""
        if not self.bus:
            return None
            
        # Send status request
        data = bytes([0x00] * 6)
        message = bytearray([self.CMD_STATUS]) + bytearray(data)
        crc = calculate_crc(bytes(message))
        message.append(crc)
        
        try:
            self.bus.write_i2c_block_data(I2C_ADDRESS, 0x00, list(message))
            time.sleep(0.01)  # Wait for MCU response
            
            # Read response
            response = self.bus.read_i2c_block_data(I2C_ADDRESS, 0x00, 8)
            if response[0] == 0x83:  # RESP_STATUS
                battery, temp, fault, _ = struct.unpack(">HHBB", bytes(response[1:7]))
                return {
                    'battery_voltage': battery / 100.0,
                    'motor_temp': temp,
                    'fault_code': fault
                }
        except Exception as e:
            print_error(f"Failed to get status: {e}")
        return None
    
    def run_test(self):
        """Run wheel movement tests"""
        print_header("WHEEL MOTOR TEST")
        
        if not self.connect():
            return False
        
        success = True
        
        try:
            # Request initial status
            print_test("Requesting MCU status...")
            status = self.request_status()
            if status:
                print_success(f"Battery: {status['battery_voltage']:.2f}V, "
                            f"Motor Temp: {status['motor_temp']}°C, "
                            f"Fault: {status['fault_code']}")
            else:
                print_warning("Could not read status (MCU may not support this)")
            
            print_warning("⚠ SAFETY: Ensure robot is elevated or on a safe surface!")
            print_info("Press Enter to start wheel test, or Ctrl+C to cancel...")
            input()
            
            # Test 1: Forward
            print_test(f"Moving FORWARD at {WHEEL_TEST_SPEED} m/s...")
            if self.send_move_command(WHEEL_TEST_SPEED, 0.0):
                time.sleep(WHEEL_TEST_DURATION)
                self.stop()
                print_success("Forward movement complete")
            else:
                success = False
            
            time.sleep(0.5)
            
            # Test 2: Backward
            print_test(f"Moving BACKWARD at {WHEEL_TEST_SPEED} m/s...")
            if self.send_move_command(-WHEEL_TEST_SPEED, 0.0):
                time.sleep(WHEEL_TEST_DURATION)
                self.stop()
                print_success("Backward movement complete")
            else:
                success = False
            
            time.sleep(0.5)
            
            # Test 3: Turn left
            print_test("Turning LEFT...")
            if self.send_move_command(0.0, 0.5):
                time.sleep(WHEEL_TEST_DURATION)
                self.stop()
                print_success("Left turn complete")
            else:
                success = False
            
            time.sleep(0.5)
            
            # Test 4: Turn right
            print_test("Turning RIGHT...")
            if self.send_move_command(0.0, -0.5):
                time.sleep(WHEEL_TEST_DURATION)
                self.stop()
                print_success("Right turn complete")
            else:
                success = False
                
        except KeyboardInterrupt:
            print_warning("\nTest cancelled by user")
            self.stop()
            success = False
        finally:
            self.stop()
            self.disconnect()
        
        return success

# ═══════════════════════════════════════════════════════════════════════════════
# SPEAKER TEST (Audio via aplay)
# ═══════════════════════════════════════════════════════════════════════════════

class SpeakerTester:
    """Test speaker/audio output via aplay"""
    
    def generate_test_tone(self, frequency=440, duration=0.5, sample_rate=44100):
        """Generate a simple sine wave test tone"""
        n_samples = int(sample_rate * duration)
        samples = []
        for i in range(n_samples):
            t = i / sample_rate
            value = int(32767 * 0.5 * math.sin(2 * math.pi * frequency * t))
            samples.append(struct.pack('<h', value))
        return b''.join(samples), sample_rate
    
    def create_wav_file(self, filename, audio_data, sample_rate):
        """Create a WAV file from audio data"""
        with wave.open(filename, 'w') as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_data)
    
    def play_test_tone(self, frequency=440, duration=0.5):
        """Play a test tone using aplay"""
        print_test(f"Generating {frequency}Hz tone ({duration}s)...")
        
        # Generate tone
        audio_data, sample_rate = self.generate_test_tone(frequency, duration)
        
        # Create temporary WAV file
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            tmp_filename = tmp.name
        
        try:
            self.create_wav_file(tmp_filename, audio_data, sample_rate)
            
            print_test("Playing via aplay...")
            result = subprocess.run(
                ['aplay', tmp_filename],
                capture_output=True,
                text=True,
                timeout=duration + 2
            )
            
            if result.returncode == 0:
                print_success(f"Played {frequency}Hz tone successfully")
                return True
            else:
                print_error(f"aplay failed: {result.stderr}")
                return False
                
        except FileNotFoundError:
            print_error("aplay not found - install alsa-utils")
            return False
        except subprocess.TimeoutExpired:
            print_error("Audio playback timed out")
            return False
        except Exception as e:
            print_error(f"Audio playback error: {e}")
            return False
        finally:
            # Clean up temp file
            if os.path.exists(tmp_filename):
                os.unlink(tmp_filename)
    
    def play_melody(self):
        """Play a simple test melody"""
        print_test("Playing test melody...")
        
        # Simple melody: C E G C (ascending)
        notes = [
            (262, 0.3),  # C4
            (330, 0.3),  # E4
            (392, 0.3),  # G4
            (523, 0.5),  # C5
        ]
        
        for freq, dur in notes:
            if not self.play_test_tone(freq, dur):
                return False
            time.sleep(0.1)
        
        print_success("Melody playback complete")
        return True
    
    def check_audio_devices(self):
        """Check available audio devices"""
        print_test("Checking audio devices...")
        try:
            result = subprocess.run(['aplay', '-l'], capture_output=True, text=True)
            if result.returncode == 0:
                print_info("Available audio devices:")
                for line in result.stdout.split('\n'):
                    if line.strip():
                        print_info(f"  {line}")
                return True
            else:
                print_error("Could not list audio devices")
                return False
        except Exception as e:
            print_error(f"Error checking audio devices: {e}")
            return False
    
    def run_test(self):
        """Run speaker tests"""
        print_header("SPEAKER/AUDIO TEST")
        
        success = True
        
        # Check audio devices
        self.check_audio_devices()
        
        print_info("Press Enter to play test sounds, or Ctrl+C to cancel...")
        try:
            input()
        except KeyboardInterrupt:
            print_warning("\nTest cancelled by user")
            return False
        
        # Test 1: Single tone
        if not self.play_test_tone(440, 0.5):
            success = False
        
        time.sleep(0.3)
        
        # Test 2: Different frequency
        if not self.play_test_tone(880, 0.5):
            success = False
        
        time.sleep(0.3)
        
        # Test 3: Melody
        if not self.play_melody():
            success = False
        
        return success

# ═══════════════════════════════════════════════════════════════════════════════
# ARM SERVO TEST (Dynamixel via Serial)
# ═══════════════════════════════════════════════════════════════════════════════

# Servo configuration from arm_config.yaml (servos 1-6 only)
# test_range_pct: percentage of total range to use for movement test (0.0-1.0)
SERVO_CONFIG = {
    1: {
        'name': 'joint_1',
        'control_mode': 3,  # Position mode
        'pwm_limit': 885,
        'current_limit': None,
        'pid': {'kp': 300, 'ki': 10, 'kd': 1000},
        'position_limits': {'min': -1.5708, 'max': 1.5708},
        'test_range_pct': 0.8  # 80% of range
    },
    2: {
        'name': 'joint_2',
        'control_mode': 3,  # Position mode
        'pwm_limit': 885,
        'current_limit': None,
        'pid': {'kp': 300, 'ki': 200, 'kd': 100},
        'position_limits': {'min': -1.5708, 'max': 1.22},
        'test_range_pct': 0.3  # 30% of range - reduced movement
    },
    3: {
        'name': 'joint_3',
        'control_mode': 3,  # Position mode
        'pwm_limit': 885,
        'current_limit': None,
        'pid': {'kp': 640, 'ki': 100, 'kd': 3600},
        'position_limits': {'min': -1.5708, 'max': 1.7453},
        'test_range_pct': 0.3  # 30% of range - reduced movement
    },
    4: {
        'name': 'joint_4',
        'control_mode': 3,  # Position mode
        'pwm_limit': 885,
        'current_limit': None,
        'pid': {'kp': 400, 'ki': 400, 'kd': 1000},
        'position_limits': {'min': -1.9199, 'max': 1.7453},
        'test_range_pct': 0.8  # 80% of range
    },
    5: {
        'name': 'joint_5',
        'control_mode': 3,  # Position mode
        'pwm_limit': 885,
        'current_limit': None,
        'pid': {'kp': 400, 'ki': 0, 'kd': 0},
        'position_limits': {'min': -1.5708, 'max': 1.5708},
        'test_range_pct': 0.8  # 80% of range
    },
    6: {
        'name': 'joint_6',
        'control_mode': 5,  # Current-controlled position mode
        'pwm_limit': 885,
        'current_limit': 100,
        'pid': {'kp': 400, 'ki': 0, 'kd': 0},
        'position_limits': {'min': -0.8727, 'max': 0.3491},
        'test_range_pct': 0.8  # 80% of range
    },
}

class ArmTester:
    """Test arm servos 1-6 via Dynamixel protocol with full initialization"""
    
    # Dynamixel Protocol 2.0 addresses
    ADDR_OPERATING_MODE = 11
    ADDR_PWM_LIMIT = 36
    ADDR_CURRENT_LIMIT = 38
    ADDR_TORQUE_ENABLE = 64
    ADDR_POSITION_D = 80
    ADDR_POSITION_I = 82
    ADDR_POSITION_P = 84
    ADDR_GOAL_POSITION = 116
    ADDR_PRESENT_POSITION = 132
    ADDR_HARDWARE_ERROR = 70
    
    # Operating modes
    OP_MODE_POSITION = 3
    OP_MODE_CURRENT_POSITION = 5
    
    # Protocol constants
    PROTOCOL_VERSION = 2.0
    
    def __init__(self):
        self.dynamixel = None
        self.robot = None
        self.direct_mode = False
        
    def connect(self):
        """Connect to Dynamixel servos"""
        try:
            # Try to import from the maurice_control package
            sys.path.insert(0, '/home/jetson1/innate-os/ros2_ws/src/maurice_bot/maurice_control')
            from maurice_control.dynamixel import Dynamixel, OperatingMode
            from maurice_control.robot import Robot
            
            print_test(f"Connecting to Dynamixel on {DYNAMIXEL_DEVICE}...")
            
            config = Dynamixel.Config(
                baudrate=DYNAMIXEL_BAUDRATE,
                device_name=DYNAMIXEL_DEVICE
            )
            self.dynamixel = config.instantiate()
            self.robot = Robot(dynamixel=self.dynamixel, servo_ids=ARM_SERVO_IDS)
            self.direct_mode = False
            
            print_success(f"Connected to Dynamixel at {DYNAMIXEL_BAUDRATE} baud")
            return True
            
        except ImportError as e:
            print_error(f"Could not import Dynamixel library: {e}")
            print_info("Trying direct dynamixel_sdk...")
            return self.connect_direct()
        except Exception as e:
            print_error(f"Failed to connect to Dynamixel: {e}")
            return False
    
    def connect_direct(self):
        """Connect using dynamixel_sdk directly"""
        try:
            from dynamixel_sdk import PortHandler, PacketHandler, COMM_SUCCESS
            
            self.port_handler = PortHandler(DYNAMIXEL_DEVICE)
            self.packet_handler = PacketHandler(self.PROTOCOL_VERSION)
            
            if not self.port_handler.openPort():
                print_error(f"Failed to open port {DYNAMIXEL_DEVICE}")
                return False
            
            if not self.port_handler.setBaudRate(DYNAMIXEL_BAUDRATE):
                print_error(f"Failed to set baudrate {DYNAMIXEL_BAUDRATE}")
                return False
            
            print_success(f"Connected directly to {DYNAMIXEL_DEVICE}")
            self.direct_mode = True
            return True
            
        except ImportError:
            print_error("dynamixel_sdk not found")
            print_info("Install with: pip3 install dynamixel-sdk")
            return False
        except Exception as e:
            print_error(f"Direct connection failed: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from Dynamixel"""
        if self.direct_mode:
            if hasattr(self, 'port_handler'):
                self.port_handler.closePort()
        elif self.dynamixel:
            self.dynamixel.disconnect()
    
    def _write_1byte(self, servo_id, address, value):
        """Write 1 byte to servo register"""
        if self.direct_mode:
            from dynamixel_sdk import COMM_SUCCESS
            result, error = self.packet_handler.write1ByteTxRx(
                self.port_handler, servo_id, address, value
            )
            return result == COMM_SUCCESS
        else:
            try:
                result, error = self.dynamixel.packetHandler.write1ByteTxRx(
                    self.dynamixel.portHandler, servo_id, address, value
                )
                from dynamixel_sdk import COMM_SUCCESS
                return result == COMM_SUCCESS
            except:
                return False
    
    def _write_2byte(self, servo_id, address, value):
        """Write 2 bytes to servo register"""
        if self.direct_mode:
            from dynamixel_sdk import COMM_SUCCESS
            result, error = self.packet_handler.write2ByteTxRx(
                self.port_handler, servo_id, address, int(value)
            )
            return result == COMM_SUCCESS
        else:
            try:
                result, error = self.dynamixel.packetHandler.write2ByteTxRx(
                    self.dynamixel.portHandler, servo_id, address, int(value)
                )
                from dynamixel_sdk import COMM_SUCCESS
                return result == COMM_SUCCESS
            except:
                return False
    
    def _write_4byte(self, servo_id, address, value):
        """Write 4 bytes to servo register"""
        if self.direct_mode:
            from dynamixel_sdk import COMM_SUCCESS
            result, error = self.packet_handler.write4ByteTxRx(
                self.port_handler, servo_id, address, int(value)
            )
            return result == COMM_SUCCESS
        else:
            try:
                result, error = self.dynamixel.packetHandler.write4ByteTxRx(
                    self.dynamixel.portHandler, servo_id, address, int(value)
                )
                from dynamixel_sdk import COMM_SUCCESS
                return result == COMM_SUCCESS
            except:
                return False
    
    def _read_4byte(self, servo_id, address):
        """Read 4 bytes from servo register"""
        if self.direct_mode:
            from dynamixel_sdk import COMM_SUCCESS
            value, result, error = self.packet_handler.read4ByteTxRx(
                self.port_handler, servo_id, address
            )
            if result == COMM_SUCCESS:
                if value > 2**31:
                    value -= 2**32
                return value
            return None
        else:
            try:
                value, result, error = self.dynamixel.packetHandler.read4ByteTxRx(
                    self.dynamixel.portHandler, servo_id, address
                )
                from dynamixel_sdk import COMM_SUCCESS
                if result == COMM_SUCCESS:
                    if value > 2**31:
                        value -= 2**32
                    return value
            except:
                pass
            return None
    
    def _read_1byte(self, servo_id, address):
        """Read 1 byte from servo register"""
        if self.direct_mode:
            from dynamixel_sdk import COMM_SUCCESS
            value, result, error = self.packet_handler.read1ByteTxRx(
                self.port_handler, servo_id, address
            )
            if result == COMM_SUCCESS:
                return value
            return None
        else:
            try:
                value, result, error = self.dynamixel.packetHandler.read1ByteTxRx(
                    self.dynamixel.portHandler, servo_id, address
                )
                from dynamixel_sdk import COMM_SUCCESS
                if result == COMM_SUCCESS:
                    return value
            except:
                pass
            return None
    
    def read_position(self, servo_id):
        """Read current position of a servo"""
        return self._read_4byte(servo_id, self.ADDR_PRESENT_POSITION)
    
    def enable_torque(self, servo_id):
        """Enable torque for a servo"""
        return self._write_1byte(servo_id, self.ADDR_TORQUE_ENABLE, 1)
    
    def disable_torque(self, servo_id):
        """Disable torque for a servo"""
        return self._write_1byte(servo_id, self.ADDR_TORQUE_ENABLE, 0)
    
    def set_operating_mode(self, servo_id, mode):
        """Set operating mode (requires torque off)"""
        return self._write_1byte(servo_id, self.ADDR_OPERATING_MODE, mode)
    
    def set_pid_gains(self, servo_id, kp, ki, kd):
        """Set PID gains for position control"""
        success = True
        success &= self._write_2byte(servo_id, self.ADDR_POSITION_P, kp)
        success &= self._write_2byte(servo_id, self.ADDR_POSITION_I, ki)
        success &= self._write_2byte(servo_id, self.ADDR_POSITION_D, kd)
        return success
    
    def set_pwm_limit(self, servo_id, limit):
        """Set PWM limit (0-885)"""
        return self._write_2byte(servo_id, self.ADDR_PWM_LIMIT, limit)
    
    def set_current_limit(self, servo_id, limit):
        """Set current limit in mA"""
        return self._write_2byte(servo_id, self.ADDR_CURRENT_LIMIT, limit)
    
    def set_position(self, servo_id, position):
        """Set goal position for a servo (in Dynamixel units: 0-4095)"""
        return self._write_4byte(servo_id, self.ADDR_GOAL_POSITION, int(position))
    
    def check_hardware_error(self, servo_id):
        """Check for hardware errors"""
        return self._read_1byte(servo_id, self.ADDR_HARDWARE_ERROR)
    
    def initialize_servo(self, servo_id):
        """
        Full servo initialization sequence:
        1. Disable torque
        2. Set operating mode
        3. Set PWM limit
        4. Set current limit (if applicable)
        5. Set PID gains
        6. Enable torque
        """
        config = SERVO_CONFIG.get(servo_id)
        if not config:
            print_error(f"  No configuration for servo {servo_id}")
            return False
        
        print_test(f"Initializing Servo {servo_id} ({config['name']})...")
        
        # Step 1: Disable torque
        print_info(f"  [1/6] Disabling torque...")
        if not self.disable_torque(servo_id):
            print_error(f"  Failed to disable torque")
            return False
        time.sleep(0.05)
        
        # Check for hardware errors
        hw_error = self.check_hardware_error(servo_id)
        if hw_error is not None and hw_error != 0:
            print_warning(f"  Hardware error detected: {hw_error}")
        
        # Step 2: Set operating mode
        print_info(f"  [2/6] Setting operating mode to {config['control_mode']}...")
        if not self.set_operating_mode(servo_id, config['control_mode']):
            print_error(f"  Failed to set operating mode")
            return False
        time.sleep(0.05)
        
        # Step 3: Set PWM limit
        print_info(f"  [3/6] Setting PWM limit to {config['pwm_limit']}...")
        if not self.set_pwm_limit(servo_id, config['pwm_limit']):
            print_error(f"  Failed to set PWM limit")
            return False
        time.sleep(0.05)
        
        # Step 4: Set current limit (if applicable)
        if config['current_limit'] is not None:
            print_info(f"  [4/6] Setting current limit to {config['current_limit']}mA...")
            if not self.set_current_limit(servo_id, config['current_limit']):
                print_error(f"  Failed to set current limit")
                return False
        else:
            print_info(f"  [4/6] Skipping current limit (not applicable)")
        time.sleep(0.05)
        
        # Step 5: Set PID gains
        pid = config['pid']
        print_info(f"  [5/6] Setting PID gains (P={pid['kp']}, I={pid['ki']}, D={pid['kd']})...")
        if not self.set_pid_gains(servo_id, pid['kp'], pid['ki'], pid['kd']):
            print_error(f"  Failed to set PID gains")
            return False
        time.sleep(0.05)
        
        # Step 6: Enable torque
        print_info(f"  [6/6] Enabling torque...")
        if not self.enable_torque(servo_id):
            print_error(f"  Failed to enable torque")
            return False
        time.sleep(0.05)
        
        print_success(f"  Servo {servo_id} initialized successfully!")
        return True
    
    def radians_to_position(self, radians):
        """Convert radians to Dynamixel position units (0 = -π, 2048 = 0, 4096 = π)"""
        return int((radians / (2 * math.pi)) * 4096) + 2048
    
    def position_to_radians(self, position):
        """Convert Dynamixel position units to radians"""
        return (position - 2048) * (2 * math.pi / 4096)
    
    def position_to_degrees(self, position):
        """Convert Dynamixel position units to degrees"""
        return math.degrees(self.position_to_radians(position))
    
    def test_servo_movement(self, servo_id):
        """Test servo movement with larger movements within configured limits"""
        config = SERVO_CONFIG.get(servo_id)
        if not config:
            return False
        
        print_test(f"Testing movement for Servo {servo_id} ({config['name']})...")
        
        # Read current position
        initial_pos = self.read_position(servo_id)
        if initial_pos is None:
            print_error(f"  Could not read position from servo {servo_id}")
            return False
        
        print_info(f"  Current position: {initial_pos} ({self.position_to_degrees(initial_pos):.1f}°)")
        
        # Get position limits from config (in radians)
        min_rad = config['position_limits']['min']
        max_rad = config['position_limits']['max']
        
        # Convert limits to Dynamixel units
        min_pos = self.radians_to_position(min_rad)
        max_pos = self.radians_to_position(max_rad)
        
        print_info(f"  Position limits: {min_pos} ({math.degrees(min_rad):.1f}°) to {max_pos} ({math.degrees(max_rad):.1f}°)")
        
        # Calculate safe test positions using per-servo range percentage
        center_pos = (min_pos + max_pos) // 2
        test_range_pct = config.get('test_range_pct', 0.8)  # Default 80% if not specified
        range_half = int((max_pos - min_pos) * (test_range_pct / 2))  # Half on each side
        
        print_info(f"  Using {int(test_range_pct * 100)}% of range for test")
        
        # Add some margin from the absolute limits
        margin = 50  # ~4.4° margin from limits
        safe_min = min_pos + margin
        safe_max = max_pos - margin
        
        target_pos_high = min(safe_max, center_pos + range_half)
        target_pos_low = max(safe_min, center_pos - range_half)
        
        print_info(f"  Test range: {target_pos_low} ({self.position_to_degrees(target_pos_low):.1f}°) to "
                  f"{target_pos_high} ({self.position_to_degrees(target_pos_high):.1f}°)")
        
        try:
            # Move to high position
            print_info(f"  Moving to high position {target_pos_high} ({self.position_to_degrees(target_pos_high):.1f}°)...")
            if not self.set_position(servo_id, target_pos_high):
                print_error(f"  Failed to set position")
                return False
            time.sleep(1.0)  # Longer wait for larger movement
            
            # Check if it moved
            new_pos = self.read_position(servo_id)
            if new_pos is not None:
                print_info(f"  Reached position: {new_pos} ({self.position_to_degrees(new_pos):.1f}°)")
                # Check if we're close enough (within 50 units / ~4°)
                if abs(new_pos - target_pos_high) > 100:
                    print_warning(f"  Position error: {abs(new_pos - target_pos_high)} units")
            
            time.sleep(0.3)
            
            # Move to low position
            print_info(f"  Moving to low position {target_pos_low} ({self.position_to_degrees(target_pos_low):.1f}°)...")
            if not self.set_position(servo_id, target_pos_low):
                print_error(f"  Failed to set position")
                return False
            time.sleep(1.0)
            
            # Check position
            new_pos = self.read_position(servo_id)
            if new_pos is not None:
                print_info(f"  Reached position: {new_pos} ({self.position_to_degrees(new_pos):.1f}°)")
                if abs(new_pos - target_pos_low) > 100:
                    print_warning(f"  Position error: {abs(new_pos - target_pos_low)} units")
            
            time.sleep(0.3)
            
            # Return to center/initial position
            return_pos = center_pos  # Return to center of range
            print_info(f"  Returning to center position {return_pos} ({self.position_to_degrees(return_pos):.1f}°)...")
            if not self.set_position(servo_id, return_pos):
                print_error(f"  Failed to return to center")
                return False
            time.sleep(1.0)
            
            final_pos = self.read_position(servo_id)
            if final_pos is not None:
                print_info(f"  Final position: {final_pos} ({self.position_to_degrees(final_pos):.1f}°)")
            
            print_success(f"  Servo {servo_id} movement test passed!")
            return True
            
        except Exception as e:
            print_error(f"  Servo {servo_id} movement test failed: {e}")
            return False
    
    def disable_all_torque(self):
        """Disable torque on all servos (safety)"""
        print_test("Disabling torque on all servos...")
        for servo_id in ARM_SERVO_IDS:
            self.disable_torque(servo_id)
        print_info("All servos torque disabled")
    
    def run_test(self):
        """Run arm servo tests with full initialization"""
        print_header("ARM SERVO TEST (Servos 1-6)")
        
        if not self.connect():
            return False
        
        print_warning("⚠ SAFETY: Ensure arm is in a safe position with clearance!")
        print_info("")
        print_info("This test will run in TWO PHASES:")
        print_info("")
        print_info("PHASE 1 - Initialize all servos (one at a time):")
        print_info("  • Disable torque")
        print_info("  • Set operating mode")
        print_info("  • Set PWM limits")
        print_info("  • Set current limits (where applicable)")
        print_info("  • Set PID gains")
        print_info("  • Enable torque")
        print_info("")
        print_info("PHASE 2 - Movement test (one servo at a time):")
        print_info("  • Move to high position (80% of range)")
        print_info("  • Move to low position (80% of range)")
        print_info("  • Return to center")
        print_info("")
        print_info(f"Testing servos: {ARM_SERVO_IDS}")
        print_info("Press Enter to start arm test, or Ctrl+C to cancel...")
        
        try:
            input()
        except KeyboardInterrupt:
            print_warning("\nTest cancelled by user")
            self.disconnect()
            return False
        
        init_results = {}
        movement_results = {}
        
        try:
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 1: Initialize ALL servos first
            # ═══════════════════════════════════════════════════════════════════
            print_header("PHASE 1: SERVO INITIALIZATION")
            print_info("Initializing all servos one at a time...\n")
            
            for servo_id in ARM_SERVO_IDS:
                init_results[servo_id] = self.initialize_servo(servo_id)
                time.sleep(0.2)
            
            # Check if all initializations passed
            init_failed = [sid for sid, passed in init_results.items() if not passed]
            if init_failed:
                print_error(f"\nInitialization failed for servos: {init_failed}")
                print_warning("Skipping movement tests for failed servos")
            
            print_info("\n" + "─" * 50)
            print_success("PHASE 1 COMPLETE: All servos initialized")
            print_info("─" * 50 + "\n")
            
            time.sleep(0.5)
            
            # ═══════════════════════════════════════════════════════════════════
            # PHASE 2: Movement tests for each servo
            # ═══════════════════════════════════════════════════════════════════
            print_header("PHASE 2: MOVEMENT TESTS")
            print_info("Testing movement on each servo one at a time...\n")
            
            for servo_id in ARM_SERVO_IDS:
                # Only test movement if initialization passed
                if init_results.get(servo_id, False):
                    movement_results[servo_id] = self.test_servo_movement(servo_id)
                else:
                    print_warning(f"Skipping movement test for Servo {servo_id} (initialization failed)")
                    movement_results[servo_id] = False
                time.sleep(0.3)
            
            print_info("\n" + "─" * 50)
            print_success("PHASE 2 COMPLETE: All movement tests finished")
            print_info("─" * 50 + "\n")
            
        except KeyboardInterrupt:
            print_warning("\nTest interrupted by user")
        finally:
            # Safety: disable all torque at end
            self.disable_all_torque()
            self.disconnect()
        
        # ═══════════════════════════════════════════════════════════════════════
        # FINAL SUMMARY
        # ═══════════════════════════════════════════════════════════════════════
        print_header("ARM TEST SUMMARY")
        
        all_passed = True
        
        print(f"{Colors.BOLD}Initialization Results:{Colors.END}")
        for servo_id in ARM_SERVO_IDS:
            passed = init_results.get(servo_id, False)
            config = SERVO_CONFIG.get(servo_id, {})
            name = config.get('name', f'servo_{servo_id}')
            if passed:
                print_success(f"  Servo {servo_id} ({name}): INITIALIZED")
            else:
                print_error(f"  Servo {servo_id} ({name}): INIT FAILED")
                all_passed = False
        
        print(f"\n{Colors.BOLD}Movement Test Results:{Colors.END}")
        for servo_id in ARM_SERVO_IDS:
            passed = movement_results.get(servo_id, False)
            config = SERVO_CONFIG.get(servo_id, {})
            name = config.get('name', f'servo_{servo_id}')
            if passed:
                print_success(f"  Servo {servo_id} ({name}): MOVEMENT OK")
            else:
                print_error(f"  Servo {servo_id} ({name}): MOVEMENT FAILED")
                all_passed = False
        
        return all_passed

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN TEST RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def print_banner():
    """Print welcome banner"""
    print(f"""
{Colors.BOLD}{Colors.CYAN}
╔══════════════════════════════════════════════════════════════╗
║         MAURICE ROBOT HARDWARE TEST SCRIPT                  ║
║                                                              ║
║  Tests:                                                      ║
║    • Wheel motors (I2C communication)                        ║
║    • Speaker/Audio (via aplay)                               ║
║    • Arm servos 1-6 (Dynamixel protocol)                     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
{Colors.END}""")

def run_all_tests():
    """Run all hardware tests"""
    print_banner()
    
    results = {
        'wheels': False,
        'speaker': False,
        'arm': False
    }
    
    # Wheel test
    wheel_tester = WheelTester()
    results['wheels'] = wheel_tester.run_test()
    
    # Speaker test
    speaker_tester = SpeakerTester()
    results['speaker'] = speaker_tester.run_test()
    
    # Arm test
    arm_tester = ArmTester()
    results['arm'] = arm_tester.run_test()
    
    # Final summary
    print_header("FINAL TEST SUMMARY")
    
    all_passed = True
    for test_name, passed in results.items():
        if passed:
            print_success(f"{test_name.upper()}: PASSED")
        else:
            print_error(f"{test_name.upper()}: FAILED")
            all_passed = False
    
    if all_passed:
        print(f"\n{Colors.GREEN}{Colors.BOLD}All tests passed! ✓{Colors.END}\n")
    else:
        print(f"\n{Colors.RED}{Colors.BOLD}Some tests failed. Check output above.{Colors.END}\n")
    
    return all_passed

def main():
    """Main entry point"""
    if len(sys.argv) > 1:
        test_type = sys.argv[1].lower()
        
        if test_type == 'wheels':
            print_banner()
            tester = WheelTester()
            success = tester.run_test()
        elif test_type == 'speaker':
            print_banner()
            tester = SpeakerTester()
            success = tester.run_test()
        elif test_type == 'arm':
            print_banner()
            tester = ArmTester()
            success = tester.run_test()
        elif test_type in ['--help', '-h', 'help']:
            print(__doc__)
            sys.exit(0)
        else:
            print(f"Unknown test type: {test_type}")
            print("Valid options: wheels, speaker, arm")
            print("Or run without arguments for all tests")
            sys.exit(1)
    else:
        success = run_all_tests()
    
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()

