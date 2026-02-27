#include "maurice_arm/robot.hpp"
#include <iostream>
#include <chrono>
#include <thread>

using namespace dynamixel;

namespace maurice_arm {

Robot::Robot(std::shared_ptr<Dynamixel> dynamixel, const std::vector<int>& servo_ids)
    : dynamixel_(dynamixel), servo_ids_(servo_ids) {
    
    // Initialize GroupSyncRead for positions
    position_reader_ = std::make_unique<GroupSyncRead>(
        dynamixel_->portHandler(),
        dynamixel_->packetHandler(),
        ADDR_PRESENT_POSITION,
        4  // 4 bytes for position
    );
    
    for (int id : servo_ids_) {
        position_reader_->addParam(id);
    }
    
    // Initialize GroupSyncRead for velocities
    velocity_reader_ = std::make_unique<GroupSyncRead>(
        dynamixel_->portHandler(),
        dynamixel_->packetHandler(),
        ADDR_PRESENT_VELOCITY,
        4  // 4 bytes for velocity
    );
    
    for (int id : servo_ids_) {
        velocity_reader_->addParam(id);
    }
    
    // Initialize GroupSyncRead for combined state (velocity + position in one transaction)
    state_reader_ = std::make_unique<GroupSyncRead>(
        dynamixel_->portHandler(),
        dynamixel_->packetHandler(),
        ADDR_PRESENT_LOAD,  // Start at load (126)
        10  // 10 bytes: 2 load + 4 velocity + 4 position
    );
    
    for (int id : servo_ids_) {
        state_reader_->addParam(id);
    }
    
    // Initialize GroupSyncWrite for positions
    pos_writer_ = std::make_unique<GroupSyncWrite>(
        dynamixel_->portHandler(),
        dynamixel_->packetHandler(),
        ADDR_GOAL_POSITION,
        4  // 4 bytes for position
    );
    
    // Initialize parameters for each servo with dummy data
    // This is required before using changeParam()
    for (int id : servo_ids_) {
        uint8_t dummy_param[4] = {0, 0, 0, 0};
        pos_writer_->addParam(id, dummy_param);
    }
}

Robot::~Robot() {
    // Cleanup is automatic with unique_ptr
}

std::vector<int> Robot::readPosition(int tries) {
    int result = position_reader_->txRxPacket();
    
    if (result != COMM_SUCCESS) {
        if (tries > 0) {
            return readPosition(tries - 1);
        } else {
            std::cerr << "Failed to read position!" << std::endl;
            return std::vector<int>(servo_ids_.size(), 0);
        }
    }
    
    std::vector<int> positions;
    for (int id : servo_ids_) {
        uint32_t position = position_reader_->getData(id, ADDR_PRESENT_POSITION, 4);
        
        // Convert to signed
        int signed_position = static_cast<int>(position);
        if (signed_position > (1LL << 31)) {
            signed_position -= (1LL << 32);
        }
        
        positions.push_back(signed_position);
    }
    
    return positions;
}

std::vector<int> Robot::readVelocity() {
    velocity_reader_->txRxPacket();
    
    std::vector<int> velocities;
    for (int id : servo_ids_) {
        uint32_t velocity = velocity_reader_->getData(id, ADDR_PRESENT_VELOCITY, 4);
        
        // Convert to signed
        int signed_velocity = static_cast<int>(velocity);
        if (signed_velocity > (1LL << 31)) {
            signed_velocity -= (1LL << 32);
        }
        
        velocities.push_back(signed_velocity);
    }
    
    return velocities;
}

std::tuple<std::vector<int>, std::vector<int>, std::vector<int>> Robot::readState(int tries) {
    auto t0 = std::chrono::steady_clock::now();
    int result = state_reader_->txRxPacket();
    last_read_txrx_us = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - t0).count();
    
    if (result != COMM_SUCCESS) {
        if (tries > 0) {
            return readState(tries - 1);
        } else {
            std::cerr << "Failed to read state!" << std::endl;
            auto z = std::vector<int>(servo_ids_.size(), 0);
            return {z, z, z};
        }
    }
    
    std::vector<int> positions;
    std::vector<int> velocities;
    std::vector<int> loads;
    
    for (int id : servo_ids_) {
        // Load: 2 bytes at address 126 (signed 16-bit)
        int16_t load = static_cast<int16_t>(state_reader_->getData(id, ADDR_PRESENT_LOAD, 2));
        loads.push_back(static_cast<int>(load));
        
        // Velocity: 4 bytes at address 128
        uint32_t velocity = state_reader_->getData(id, ADDR_PRESENT_VELOCITY, 4);
        int signed_velocity = static_cast<int>(velocity);
        if (signed_velocity > (1LL << 31)) {
            signed_velocity -= (1LL << 32);
        }
        velocities.push_back(signed_velocity);
        
        // Position: 4 bytes at address 132
        uint32_t position = state_reader_->getData(id, ADDR_PRESENT_POSITION, 4);
        int signed_position = static_cast<int>(position);
        if (signed_position > (1LL << 31)) {
            signed_position -= (1LL << 32);
        }
        positions.push_back(signed_position);
    }
    
    return {positions, velocities, loads};
}

void Robot::setGoalPos(const std::vector<int>& action) {
    if (action.size() != servo_ids_.size()) {
        throw std::invalid_argument("Action size must match number of servos");
    }
    
    // Add parameters for each servo
    for (size_t i = 0; i < servo_ids_.size(); ++i) {
        uint8_t param[4];
        param[0] = DXL_LOBYTE(DXL_LOWORD(action[i]));
        param[1] = DXL_HIBYTE(DXL_LOWORD(action[i]));
        param[2] = DXL_LOBYTE(DXL_HIWORD(action[i]));
        param[3] = DXL_HIBYTE(DXL_HIWORD(action[i]));
        
        pos_writer_->changeParam(servo_ids_[i], param);
    }
    
    // Send packet
    auto t0 = std::chrono::steady_clock::now();
    pos_writer_->txPacket();
    last_write_txrx_us = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - t0).count();
}

void Robot::rebootAllServos() {
    for (int id : servo_ids_) {
        dynamixel_->reboot(id);
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }
    
    // Give servos time to complete reboot before next command
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
}

} // namespace maurice_arm

