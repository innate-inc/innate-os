#pragma once

#include <string>
#include <cstdint>
#include "dynamixel_sdk/dynamixel_sdk.h"

namespace maurice_arm {

enum class OperatingMode {
    VELOCITY = 1,
    POSITION = 3,
    CURRENT_CONTROLLED_POSITION = 5,
    PWM = 16
};

class Dynamixel {
public:
    struct Config {
        int baudrate = 57600;
        float protocol_version = 2.0;
        std::string device_name = "";
        int dynamixel_id = 1;
    };

    explicit Dynamixel(const Config& config);
    ~Dynamixel();

    // Torque control
    void enableTorque(int motor_id);
    void disableTorque(int motor_id);
    
    // Configuration
    void setOperatingMode(int motor_id, OperatingMode mode);
    void setMinPositionLimit(int motor_id, int min_position);
    void setMaxPositionLimit(int motor_id, int max_position);
    void setPwmLimit(int motor_id, int limit);
    void setCurrentLimit(int motor_id, int current_limit);
    void setP(int motor_id, int p);
    void setI(int motor_id, int i);
    void setD(int motor_id, int d);
    // Write D, I, P gains to multiple servos in a single GroupSyncWrite packet
    // Each entry: {servo_id, kd, ki, kp}
    void syncWritePID(const std::vector<std::tuple<int, int, int, int>>& pid_data);
    void setHomeOffset(int motor_id, int home_position);
    void setProfileVelocity(int motor_id, int velocity);
    void setProfileAcceleration(int motor_id, int acceleration);
    // Write profile acceleration + velocity to multiple servos in a single GroupSyncWrite packet
    // Each entry: {servo_id, profile_acceleration, profile_velocity}
    void syncWriteProfile(const std::vector<std::tuple<int, int, int>>& profile_data);
    
    // Read/Write
    int readPosition(int motor_id);
    int readVelocity(int motor_id);
    void setGoalPosition(int motor_id, int goal_position);
    void reboot(int motor_id);
    uint8_t readHardwareErrorStatus(int motor_id);
    int16_t readPresentLoad(int motor_id);
    uint8_t readPresentTemperature(int motor_id);
    
    // Access to SDK objects for GroupSync operations
    dynamixel::PortHandler* portHandler() { return port_handler_; }
    dynamixel::PacketHandler* packetHandler() { return packet_handler_; }

private:
    void connect();
    
    Config config_;
    dynamixel::PortHandler* port_handler_;
    dynamixel::PacketHandler* packet_handler_;
    
    // Control table addresses
    static constexpr int ADDR_TORQUE_ENABLE = 64;
    static constexpr int ADDR_GOAL_POSITION = 116;
    static constexpr int ADDR_PWM_LIMIT = 36;
    static constexpr int OPERATING_MODE_ADDR = 11;
    static constexpr int POSITION_I = 82;
    static constexpr int POSITION_P = 84;
    static constexpr int POSITION_D = 80;
    static constexpr int ADDR_MIN_POSITION_LIMIT = 52;
    static constexpr int ADDR_MAX_POSITION_LIMIT = 48;
    static constexpr int ADDR_CURRENT_LIMIT = 38;
    static constexpr int ADDR_PRESENT_POSITION = 132;
    static constexpr int ADDR_PRESENT_VELOCITY = 128;
    static constexpr int ADDR_HOMING_OFFSET = 20;
    static constexpr int ADDR_HARDWARE_ERROR_STATUS = 70;
    static constexpr int ADDR_PRESENT_LOAD = 126;
    static constexpr int ADDR_PROFILE_ACCELERATION = 108;
    static constexpr int ADDR_PROFILE_VELOCITY = 112;
    static constexpr int ADDR_PRESENT_TEMPERATURE = 146;
};

} // namespace maurice_arm

