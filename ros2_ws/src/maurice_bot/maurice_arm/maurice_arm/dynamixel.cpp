#include "maurice_arm/dynamixel.hpp"
#include <iostream>
#include <glob.h>
#include <stdexcept>
#include <thread>
#include <chrono>

using namespace dynamixel;

namespace maurice_arm {

Dynamixel::Dynamixel(const Config& config) : config_(config) {
    connect();
}

Dynamixel::~Dynamixel() {
    if (port_handler_) {
        port_handler_->closePort();
    }
}

void Dynamixel::connect() {
    // Auto-detect device if not specified
    if (config_.device_name.empty()) {
        glob_t glob_result;
        glob("/dev/ttyUSB*", GLOB_TILDE, NULL, &glob_result);
        glob("/dev/ttyACM*", GLOB_APPEND | GLOB_TILDE, NULL, &glob_result);
        
        if (glob_result.gl_pathc > 0) {
            config_.device_name = glob_result.gl_pathv[0];
            std::cout << "Using device: " << config_.device_name << std::endl;
        }
        globfree(&glob_result);
    }
    
    port_handler_ = PortHandler::getPortHandler(config_.device_name.c_str());
    packet_handler_ = PacketHandler::getPacketHandler(config_.protocol_version);
    
    if (!port_handler_->openPort()) {
        throw std::runtime_error("Failed to open port " + config_.device_name);
    }
    
    if (!port_handler_->setBaudRate(config_.baudrate)) {
        throw std::runtime_error("Failed to set baudrate to " + std::to_string(config_.baudrate));
    }
}

void Dynamixel::enableTorque(int motor_id) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write1ByteTxRx(
        port_handler_, motor_id, ADDR_TORQUE_ENABLE, 1, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to enable torque for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::disableTorque(int motor_id) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write1ByteTxRx(
        port_handler_, motor_id, ADDR_TORQUE_ENABLE, 0, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to disable torque for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setOperatingMode(int motor_id, OperatingMode mode) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write1ByteTxRx(
        port_handler_, motor_id, OPERATING_MODE_ADDR, static_cast<uint8_t>(mode), &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set operating mode for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setMinPositionLimit(int motor_id, int min_position) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_MIN_POSITION_LIMIT, min_position, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set min position limit for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setMaxPositionLimit(int motor_id, int max_position) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_MAX_POSITION_LIMIT, max_position, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set max position limit for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setPwmLimit(int motor_id, int limit) {
    if (limit < 0 || limit > 885) {
        throw std::invalid_argument("PWM limit must be between 0 and 885");
    }
    
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write2ByteTxRx(
        port_handler_, motor_id, ADDR_PWM_LIMIT, limit, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set PWM limit for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setCurrentLimit(int motor_id, int current_limit) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write2ByteTxRx(
        port_handler_, motor_id, ADDR_CURRENT_LIMIT, current_limit, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set current limit for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setP(int motor_id, int p) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write2ByteTxRx(
        port_handler_, motor_id, POSITION_P, p, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set P gain for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setI(int motor_id, int i) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write2ByteTxRx(
        port_handler_, motor_id, POSITION_I, i, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set I gain for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setD(int motor_id, int d) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write2ByteTxRx(
        port_handler_, motor_id, POSITION_D, d, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set D gain for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::syncWritePID(const std::vector<std::tuple<int, int, int, int>>& pid_data) {
    // Addresses 80-85: D(2 bytes) + I(2 bytes) + P(2 bytes) = 6 bytes contiguous
    dynamixel::GroupSyncWrite syncWrite(port_handler_, packet_handler_, POSITION_D, 6);
    
    for (const auto& [servo_id, kd, ki, kp] : pid_data) {
        uint8_t param[6];
        // D gain at offset 0 (addr 80), little-endian
        param[0] = DXL_LOBYTE(static_cast<uint16_t>(kd));
        param[1] = DXL_HIBYTE(static_cast<uint16_t>(kd));
        // I gain at offset 2 (addr 82), little-endian
        param[2] = DXL_LOBYTE(static_cast<uint16_t>(ki));
        param[3] = DXL_HIBYTE(static_cast<uint16_t>(ki));
        // P gain at offset 4 (addr 84), little-endian
        param[4] = DXL_LOBYTE(static_cast<uint16_t>(kp));
        param[5] = DXL_HIBYTE(static_cast<uint16_t>(kp));
        
        if (!syncWrite.addParam(servo_id, param)) {
            throw std::runtime_error("Failed to add PID param for servo " + std::to_string(servo_id));
        }
    }
    
    int dxl_comm_result = syncWrite.txPacket();
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("SyncWrite PID failed: " + std::string(packet_handler_->getTxRxResult(dxl_comm_result)));
    }
    syncWrite.clearParam();
}

void Dynamixel::syncWriteProfile(const std::vector<std::tuple<int, int, int>>& profile_data) {
    // Addresses 108-115: Profile Acceleration(4 bytes) + Profile Velocity(4 bytes) = 8 bytes contiguous
    dynamixel::GroupSyncWrite syncWrite(port_handler_, packet_handler_, ADDR_PROFILE_ACCELERATION, 8);
    
    for (const auto& [servo_id, accel, vel] : profile_data) {
        uint8_t param[8];
        // Profile Acceleration at offset 0 (addr 108), little-endian 4 bytes
        param[0] = DXL_LOBYTE(DXL_LOWORD(static_cast<uint32_t>(accel)));
        param[1] = DXL_HIBYTE(DXL_LOWORD(static_cast<uint32_t>(accel)));
        param[2] = DXL_LOBYTE(DXL_HIWORD(static_cast<uint32_t>(accel)));
        param[3] = DXL_HIBYTE(DXL_HIWORD(static_cast<uint32_t>(accel)));
        // Profile Velocity at offset 4 (addr 112), little-endian 4 bytes
        param[4] = DXL_LOBYTE(DXL_LOWORD(static_cast<uint32_t>(vel)));
        param[5] = DXL_HIBYTE(DXL_LOWORD(static_cast<uint32_t>(vel)));
        param[6] = DXL_LOBYTE(DXL_HIWORD(static_cast<uint32_t>(vel)));
        param[7] = DXL_HIBYTE(DXL_HIWORD(static_cast<uint32_t>(vel)));
        
        if (!syncWrite.addParam(servo_id, param)) {
            throw std::runtime_error("Failed to add profile param for servo " + std::to_string(servo_id));
        }
    }
    
    int dxl_comm_result = syncWrite.txPacket();
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("SyncWrite profile failed: " + std::string(packet_handler_->getTxRxResult(dxl_comm_result)));
    }
    syncWrite.clearParam();
}

void Dynamixel::setHomeOffset(int motor_id, int home_position) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_HOMING_OFFSET, home_position, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set home offset for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setProfileVelocity(int motor_id, int velocity) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_PROFILE_VELOCITY, velocity, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set profile velocity for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::setProfileAcceleration(int motor_id, int acceleration) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_PROFILE_ACCELERATION, acceleration, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set profile acceleration for motor " + std::to_string(motor_id));
    }
}

int Dynamixel::readPosition(int motor_id) {
    uint32_t position = 0;
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->read4ByteTxRx(
        port_handler_, motor_id, ADDR_PRESENT_POSITION, &position, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to read position for motor " + std::to_string(motor_id));
    }
    
    // Convert to signed
    int signed_position = static_cast<int>(position);
    if (signed_position > (1LL << 31)) {
        signed_position -= (1LL << 32);
    }
    
    return signed_position;
}

int Dynamixel::readVelocity(int motor_id) {
    uint32_t velocity = 0;
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->read4ByteTxRx(
        port_handler_, motor_id, ADDR_PRESENT_VELOCITY, &velocity, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to read velocity for motor " + std::to_string(motor_id));
    }
    
    // Convert to signed
    int signed_velocity = static_cast<int>(velocity);
    if (signed_velocity > (1LL << 31)) {
        signed_velocity -= (1LL << 32);
    }
    
    return signed_velocity;
}

void Dynamixel::setGoalPosition(int motor_id, int goal_position) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->write4ByteTxRx(
        port_handler_, motor_id, ADDR_GOAL_POSITION, goal_position, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to set goal position for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::reboot(int motor_id) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->reboot(
        port_handler_, motor_id, &dxl_error);
    
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to reboot motor " + std::to_string(motor_id));
    }
}

uint8_t Dynamixel::readHardwareErrorStatus(int motor_id) {
    uint8_t status = 0;
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->read1ByteTxRx(
        port_handler_, motor_id, ADDR_HARDWARE_ERROR_STATUS, &status, &dxl_error);

    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to read hardware error status for motor " + std::to_string(motor_id));
    }

    return status;
}

int16_t Dynamixel::readPresentLoad(int motor_id) {
    uint16_t load = 0;
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->read2ByteTxRx(
        port_handler_, motor_id, ADDR_PRESENT_LOAD, &load, &dxl_error);

    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to read load for motor " + std::to_string(motor_id));
    }

    return static_cast<int16_t>(load);
}

uint8_t Dynamixel::readPresentTemperature(int motor_id) {
    uint8_t temperature = 0;
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->read1ByteTxRx(
        port_handler_, motor_id, ADDR_PRESENT_TEMPERATURE, &temperature, &dxl_error);

    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to read temperature for motor " + std::to_string(motor_id));
    }

    return temperature;
}

} // namespace maurice_arm

