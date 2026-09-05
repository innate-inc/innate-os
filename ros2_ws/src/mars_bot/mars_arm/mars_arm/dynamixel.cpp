// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#include "mars_arm/dynamixel.hpp"
#include <iostream>
#include <glob.h>
#include <stdexcept>
#include <thread>
#include <chrono>

using namespace dynamixel;

namespace mars_arm {

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

void Dynamixel::checkComm(int dxl_comm_result, int motor_id, const char* op_name) {
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error(std::string("Failed to ") + op_name + " for motor " + std::to_string(motor_id));
    }
}

void Dynamixel::writeRegister(int motor_id, uint16_t address, uint32_t value, int num_bytes, const char* op_name) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = COMM_SUCCESS;
    switch (num_bytes) {
        case 1:
            dxl_comm_result = packet_handler_->write1ByteTxRx(port_handler_, motor_id, address,
                                                              static_cast<uint8_t>(value), &dxl_error);
            break;
        case 2:
            dxl_comm_result = packet_handler_->write2ByteTxRx(port_handler_, motor_id, address,
                                                              static_cast<uint16_t>(value), &dxl_error);
            break;
        case 4:
            dxl_comm_result = packet_handler_->write4ByteTxRx(port_handler_, motor_id, address, value, &dxl_error);
            break;
        default:
            throw std::invalid_argument("Unsupported register width: " + std::to_string(num_bytes));
    }
    checkComm(dxl_comm_result, motor_id, op_name);
}

int Dynamixel::toSigned32(uint32_t value) {
    // Two's-complement reinterpretation: the high bit becomes the sign.
    return static_cast<int>(value);
}

void Dynamixel::enableTorque(int motor_id) {
    writeRegister(motor_id, ADDR_TORQUE_ENABLE, 1, 1, "enable torque");
}

void Dynamixel::disableTorque(int motor_id) {
    writeRegister(motor_id, ADDR_TORQUE_ENABLE, 0, 1, "disable torque");
}

void Dynamixel::setOperatingMode(int motor_id, OperatingMode mode) {
    writeRegister(motor_id, OPERATING_MODE_ADDR, static_cast<uint8_t>(mode), 1, "set operating mode");
}

void Dynamixel::setMinPositionLimit(int motor_id, int min_position) {
    writeRegister(motor_id, ADDR_MIN_POSITION_LIMIT, min_position, 4, "set min position limit");
}

void Dynamixel::setMaxPositionLimit(int motor_id, int max_position) {
    writeRegister(motor_id, ADDR_MAX_POSITION_LIMIT, max_position, 4, "set max position limit");
}

void Dynamixel::setPwmLimit(int motor_id, int limit) {
    if (limit < 0 || limit > 885) {
        throw std::invalid_argument("PWM limit must be between 0 and 885");
    }
    writeRegister(motor_id, ADDR_PWM_LIMIT, limit, 2, "set PWM limit");
}

void Dynamixel::setCurrentLimit(int motor_id, int current_limit) {
    writeRegister(motor_id, ADDR_CURRENT_LIMIT, current_limit, 2, "set current limit");
}

void Dynamixel::setGoalCurrent(int motor_id, int goal_current) {
    writeRegister(motor_id, ADDR_GOAL_CURRENT, goal_current, 2, "set goal current");
}

void Dynamixel::setP(int motor_id, int p) {
    writeRegister(motor_id, POSITION_P, p, 2, "set P gain");
}

void Dynamixel::setI(int motor_id, int i) {
    writeRegister(motor_id, POSITION_I, i, 2, "set I gain");
}

void Dynamixel::setD(int motor_id, int d) {
    writeRegister(motor_id, POSITION_D, d, 2, "set D gain");
}

void Dynamixel::syncWritePID(const std::vector<std::tuple<int, int, int, int, int, int>>& pid_data) {
    // Addresses 80-91: D(2) + I(2) + P(2) + reserved(2) + FF2(2) + FF1(2) = 12 bytes
    // addr 86-87 is a reserved gap — we write 0x0000 there (harmless)
    dynamixel::GroupSyncWrite syncWrite(port_handler_, packet_handler_, POSITION_D, 12);

    for (const auto& [servo_id, ff2, ff1, kd, ki, kp] : pid_data) {
        uint8_t param[12];
        // Position D gain at offset 0 (addr 80)
        param[0] = DXL_LOBYTE(static_cast<uint16_t>(kd));
        param[1] = DXL_HIBYTE(static_cast<uint16_t>(kd));
        // Position I gain at offset 2 (addr 82)
        param[2] = DXL_LOBYTE(static_cast<uint16_t>(ki));
        param[3] = DXL_HIBYTE(static_cast<uint16_t>(ki));
        // Position P gain at offset 4 (addr 84)
        param[4] = DXL_LOBYTE(static_cast<uint16_t>(kp));
        param[5] = DXL_HIBYTE(static_cast<uint16_t>(kp));
        // Reserved gap at offset 6 (addr 86-87)
        param[6] = 0;
        param[7] = 0;
        // FF2 (acceleration feedforward) at offset 8 (addr 88)
        param[8] = DXL_LOBYTE(static_cast<uint16_t>(ff2));
        param[9] = DXL_HIBYTE(static_cast<uint16_t>(ff2));
        // FF1 (velocity feedforward) at offset 10 (addr 90)
        param[10] = DXL_LOBYTE(static_cast<uint16_t>(ff1));
        param[11] = DXL_HIBYTE(static_cast<uint16_t>(ff1));

        if (!syncWrite.addParam(servo_id, param)) {
            throw std::runtime_error("Failed to add PID param for servo " + std::to_string(servo_id));
        }
    }

    int dxl_comm_result = syncWrite.txPacket();
    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("SyncWrite PID failed: " +
                                 std::string(packet_handler_->getTxRxResult(dxl_comm_result)));
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
        throw std::runtime_error("SyncWrite profile failed: " +
                                 std::string(packet_handler_->getTxRxResult(dxl_comm_result)));
    }
    syncWrite.clearParam();
}

void Dynamixel::setHomeOffset(int motor_id, int home_position) {
    writeRegister(motor_id, ADDR_HOMING_OFFSET, home_position, 4, "set home offset");
}

void Dynamixel::setProfileVelocity(int motor_id, int velocity) {
    writeRegister(motor_id, ADDR_PROFILE_VELOCITY, velocity, 4, "set profile velocity");
}

void Dynamixel::setProfileAcceleration(int motor_id, int acceleration) {
    writeRegister(motor_id, ADDR_PROFILE_ACCELERATION, acceleration, 4, "set profile acceleration");
}

int Dynamixel::readPosition(int motor_id) {
    uint32_t position = 0;
    uint8_t dxl_error = 0;
    checkComm(packet_handler_->read4ByteTxRx(port_handler_, motor_id, ADDR_PRESENT_POSITION, &position, &dxl_error),
              motor_id, "read position");
    return toSigned32(position);
}

int Dynamixel::readVelocity(int motor_id) {
    uint32_t velocity = 0;
    uint8_t dxl_error = 0;
    checkComm(packet_handler_->read4ByteTxRx(port_handler_, motor_id, ADDR_PRESENT_VELOCITY, &velocity, &dxl_error),
              motor_id, "read velocity");
    return toSigned32(velocity);
}

void Dynamixel::setGoalPosition(int motor_id, int goal_position) {
    writeRegister(motor_id, ADDR_GOAL_POSITION, goal_position, 4, "set goal position");
}

void Dynamixel::reboot(int motor_id) {
    uint8_t dxl_error = 0;
    int dxl_comm_result = packet_handler_->reboot(port_handler_, motor_id, &dxl_error);

    if (dxl_comm_result != COMM_SUCCESS) {
        throw std::runtime_error("Failed to reboot motor " + std::to_string(motor_id));
    }
}

uint8_t Dynamixel::readHardwareErrorStatus(int motor_id) {
    uint8_t status = 0;
    uint8_t dxl_error = 0;
    checkComm(packet_handler_->read1ByteTxRx(port_handler_, motor_id, ADDR_HARDWARE_ERROR_STATUS, &status, &dxl_error),
              motor_id, "read hardware error status");
    return status;
}

int16_t Dynamixel::readPresentLoad(int motor_id) {
    uint16_t load = 0;
    uint8_t dxl_error = 0;
    checkComm(packet_handler_->read2ByteTxRx(port_handler_, motor_id, ADDR_PRESENT_LOAD, &load, &dxl_error), motor_id,
              "read load");
    return static_cast<int16_t>(load);
}

uint8_t Dynamixel::readPresentTemperature(int motor_id) {
    uint8_t temperature = 0;
    uint8_t dxl_error = 0;
    checkComm(
        packet_handler_->read1ByteTxRx(port_handler_, motor_id, ADDR_PRESENT_TEMPERATURE, &temperature, &dxl_error),
        motor_id, "read temperature");
    return temperature;
}

void Dynamixel::setReturnDelayTime(int motor_id, int value) {
    writeRegister(motor_id, ADDR_RETURN_DELAY_TIME, value, 1, "set return delay time");
}

}  // namespace mars_arm
