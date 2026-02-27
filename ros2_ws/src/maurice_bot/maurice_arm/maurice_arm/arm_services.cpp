// arm_services.cpp — Servo initialization, health monitoring, service callbacks, head control
#include "maurice_arm/arm_node.hpp"

using json = nlohmann::json;

namespace maurice_arm {

// ========== SERVO INITIALIZATION ==========

void MauriceArmNode::initializeServos() {
    std::lock_guard<std::mutex> lock(dynamixel_mutex_);
    configureServosLocked();
}

void MauriceArmNode::configureServoByIdLocked(int servo_id, bool enable_torque) {
    // Find the config for this servo
    const JointConfig* config_ptr = nullptr;
    size_t joint_index = 0;
    for (size_t i = 0; i < joint_configs_.size(); ++i) {
        if (joint_configs_[i].servo_id == servo_id) {
            config_ptr = &joint_configs_[i];
            joint_index = i;
            break;
        }
    }
    if (!config_ptr) {
        throw std::runtime_error("No config found for servo ID " + std::to_string(servo_id));
    }
    const auto& config = *config_ptr;

    RCLCPP_INFO(this->get_logger(), "Configuring servo %d (%s)", config.servo_id,
        config.motor_type.empty() ? "unknown" : config.motor_type.c_str());

    retryServoOp(config.servo_id, "disableTorque", [&]{ dynamixel_->disableTorque(config.servo_id); });
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    dynamixel_->setReturnDelayTime(config.servo_id, 100);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    int min_encoder = static_cast<int>((config.min_pos_rad / (2 * M_PI)) * 4096 + 2048);
    int max_encoder = static_cast<int>((config.max_pos_rad / (2 * M_PI)) * 4096 + 2048);
    dynamixel_->setMinPositionLimit(config.servo_id, min_encoder);
    dynamixel_->setMaxPositionLimit(config.servo_id, max_encoder);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    dynamixel_->setPwmLimit(config.servo_id, config.pwm_limit);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    if (config.current_limit > 0) {
        dynamixel_->setCurrentLimit(config.servo_id, config.current_limit);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    OperatingMode mode;
    switch (config.control_mode) {
        case 0:  mode = OperatingMode::CURRENT; break;
        case 1:  mode = OperatingMode::VELOCITY; break;
        case 3:  mode = OperatingMode::POSITION; break;
        case 4:  mode = OperatingMode::EXTENDED_POSITION; break;
        case 5:  mode = OperatingMode::CURRENT_CONTROLLED_POSITION; break;
        case 16: mode = OperatingMode::PWM; break;
        default: throw std::runtime_error("Servo " + std::to_string(config.servo_id) + ": invalid control_mode");
    }
    dynamixel_->setOperatingMode(config.servo_id, mode);
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    if (config.homing_offset != 0) {
        dynamixel_->setHomeOffset(config.servo_id, config.homing_offset);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    // Write PID gains for this servo
    std::vector<std::tuple<int, int, int, int, int, int>> pid_data;
    pid_data.emplace_back(config.servo_id, config.ff2, config.ff1, config.kd, config.ki, config.kp);
    dynamixel_->syncWritePID(pid_data);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    if (config.profile_velocity > 0) {
        dynamixel_->setProfileVelocity(config.servo_id, config.profile_velocity);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    if (config.profile_acceleration > 0) {
        dynamixel_->setProfileAcceleration(config.servo_id, config.profile_acceleration);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    // Reset gain scheduling tracking for this joint
    gs_last_applied_[joint_index] = gs_near_[joint_index];

    if (enable_torque) {
        RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", config.servo_id);
        retryServoOp(config.servo_id, "enableTorque", [&]{ dynamixel_->enableTorque(config.servo_id); });
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    RCLCPP_INFO(this->get_logger(), "Servo %d configured and torque %s",
        config.servo_id, enable_torque ? "enabled" : "disabled");
}

void MauriceArmNode::configureServosLocked(bool enable_torque) {
    RCLCPP_INFO(this->get_logger(), "Configuring all 7 servos...");

    for (const auto& config : joint_configs_) {
        try {
            configureServoByIdLocked(config.servo_id, enable_torque);
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to configure servo %d, skipping: %s",
                config.servo_id, e.what());
        }
    }

    // Move head to default position
    try {
        RCLCPP_INFO(this->get_logger(), "Moving head to default position (0.0 deg)");
        moveHeadToAngleLocked(0.0);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Failed to move head to default position: %s", e.what());
    }
}

// ========== POSITION SYNC HELPER ==========

void MauriceArmNode::syncTargetToMotorPositions() {
    auto [positions, velocities, loads] = robot_->readState();
    (void)velocities; (void)loads;
    {
        std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
        for (int i = 0; i < 6 && i < static_cast<int>(positions.size()); ++i) {
            double rad = ((positions[i] - 2048) * 2 * M_PI) / 4096.0;
            if (i == 1 || i == 2 || i == 3 || i == 5) rad = -rad;
            latest_target_[i] = rad;
        }
        has_target_ = false;
        latest_arm_command_ = std::vector<int>(positions.begin(), positions.begin() + 6);
    }
}

// ========== HEALTH MONITORING ==========

void MauriceArmNode::healthMonitorCallback() {
    maurice_msgs::msg::ArmStatus status_msg;
    status_msg.is_ok = true;
    status_msg.error = "All servos nominal";
    status_msg.is_torque_enabled = arm_torque_enabled_.load();

    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        for (const auto& config : joint_configs_) {
            const int servo_id = config.servo_id;

            uint8_t hw_status = dynamixel_->readHardwareErrorStatus(servo_id);
            if (hw_status != 0) {
                status_msg.is_ok = false;
                status_msg.error = describeHardwareError(hw_status, servo_id);
                break;
            }

            int16_t present_load = dynamixel_->readPresentLoad(servo_id);
            if (std::abs(static_cast<int>(present_load)) >= kLoadWarningThreshold) {
                status_msg.is_ok = false;
                double load_percent = static_cast<double>(present_load) / 10.0;
                status_msg.error = "Servo " + std::to_string(servo_id) +
                    " high load (" + std::to_string(load_percent) + "%)";
                break;
            }

            uint8_t temperature = dynamixel_->readPresentTemperature(servo_id);
            if (temperature >= kTemperatureWarningC) {
                status_msg.is_ok = false;
                status_msg.error = "Servo " + std::to_string(servo_id) +
                    " high temperature (" + std::to_string(static_cast<int>(temperature)) + " C)";
                break;
            }
        }
    } catch (const std::exception& e) {
        status_msg.is_ok = false;
        status_msg.error = std::string("Health check error: ") + e.what();
    }

    arm_status_pub_->publish(status_msg);

    if (status_msg.is_ok != last_arm_status_.is_ok || status_msg.error != last_arm_status_.error) {
        if (status_msg.is_ok) {
            RCLCPP_INFO(this->get_logger(), "Arm health nominal: %s", status_msg.error.c_str());
        } else {
            RCLCPP_ERROR(this->get_logger(), "Arm health issue: %s", status_msg.error.c_str());
        }
        last_arm_status_ = status_msg;
    }
}

std::string MauriceArmNode::describeHardwareError(uint8_t status, int servo_id) const {
    std::vector<std::string> error_bits;
    if (status & 0x20) { error_bits.emplace_back("overload"); }
    if (status & 0x10) { error_bits.emplace_back("electrical fault"); }
    if (status & 0x08) { error_bits.emplace_back("encoder fault"); }
    if (status & 0x04) { error_bits.emplace_back("overheating"); }
    if (status & 0x01) { error_bits.emplace_back("input voltage"); }

    std::ostringstream oss;
    oss << "Servo " << servo_id << " hardware error";
    if (!error_bits.empty()) {
        oss << ": ";
        for (size_t i = 0; i < error_bits.size(); ++i) {
            if (i > 0) oss << ", ";
            oss << error_bits[i];
        }
    } else {
        oss << " (code 0x" << std::hex << static_cast<int>(status) << ")";
    }
    return oss.str();
}

// ========== ARM COMMAND CALLBACK ==========

void MauriceArmNode::armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg) {
    try {
        if (msg->data.size() != 6) {
            RCLCPP_ERROR(this->get_logger(), "Action size must match number of servos. Expected 6, got %zu", msg->data.size());
            return;
        }

        std::lock_guard<std::mutex> lock(arm_command_mutex_);
        for (int i = 0; i < 6; ++i)
            latest_target_[i] = msg->data[i];
        has_target_ = true;

        // Switch to teleop gains when streaming commands arrive
        if (gain_mode_ != GainMode::TELEOP) {
            gain_mode_ = GainMode::TELEOP;
            RCLCPP_INFO(this->get_logger(), "Gain mode -> TELEOP (streaming commands detected)");
        }

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error in arm command callback: %s", e.what());
    }
}

// ========== TORQUE / REBOOT / FIX-ERROR SERVICE CALLBACKS ==========

void MauriceArmNode::armTorqueOnCallback(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/torque_on");
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        for (int id = 1; id <= 6; ++id) {
            RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", id);
            dynamixel_->enableTorque(id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        try { syncTargetToMotorPositions(); }
        catch (const std::exception& e) {
            RCLCPP_WARN(this->get_logger(), "Failed to sync on torque on: %s", e.what());
        }

        arm_torque_enabled_ = true;
        response->success = true;
        response->message = "Enabled torque for all arm servos";
        RCLCPP_INFO(this->get_logger(), "Successfully enabled torque for all arm servos");
    } catch (const std::exception& e) {
        response->success = false;
        response->message = std::string("Failed: ") + e.what();
        RCLCPP_ERROR(this->get_logger(), "Failed to enable torque: %s", e.what());
    }
}

void MauriceArmNode::armTorqueOffCallback(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/torque_off");
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        for (int id = 1; id <= 6; ++id) {
            RCLCPP_INFO(this->get_logger(), "  Disabling torque on servo %d", id);
            dynamixel_->disableTorque(id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        arm_torque_enabled_ = false;
        response->success = true;
        response->message = "Disabled torque for all arm servos";
        RCLCPP_INFO(this->get_logger(), "Successfully disabled torque for all arm servos");
    } catch (const std::exception& e) {
        response->success = false;
        response->message = std::string("Failed: ") + e.what();
        RCLCPP_ERROR(this->get_logger(), "Failed to disable torque: %s", e.what());
    }
}

void MauriceArmNode::armRebootServosCallback(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/reboot");
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        RCLCPP_INFO(this->get_logger(), "Rebooting all servos (IDs 1-7)");
        robot_->rebootAllServos();

        RCLCPP_INFO(this->get_logger(), "Reapplying configuration after reboot (arm torque off)");
        configureServosLocked(false);

        // Re-enable torque only for head servo
        RCLCPP_INFO(this->get_logger(), "Enabling torque on head servo (ID 7)");
        dynamixel_->enableTorque(7);

        arm_torque_enabled_ = false;
        response->success = true;
        response->message = "Rebooted and reinitialized all servos (arm torque off, head torque on)";
        RCLCPP_INFO(this->get_logger(), "Successfully rebooted and reinitialized all servos");
    } catch (const std::exception& e) {
        response->success = false;
        response->message = std::string("Failed: ") + e.what();
        RCLCPP_ERROR(this->get_logger(), "Failed to reboot servos: %s", e.what());
    }
}

void MauriceArmNode::armFixErrorCallback(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/fix_error");
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        // Scan all servos and collect those with hardware errors
        std::vector<int> error_servo_ids;
        for (const auto& config : joint_configs_) {
            uint8_t hw_status = dynamixel_->readHardwareErrorStatus(config.servo_id);
            if (hw_status != 0) {
                RCLCPP_WARN(this->get_logger(), "Servo %d has hardware error: %s",
                    config.servo_id, describeHardwareError(hw_status, config.servo_id).c_str());
                error_servo_ids.push_back(config.servo_id);
            }
        }

        // Build JSON array of error IDs for machine-readable response
        json error_ids_json = json::array();
        for (int id : error_servo_ids) {
            error_ids_json.push_back(id);
        }

        if (error_servo_ids.empty()) {
            json result;
            result["error_ids"] = error_ids_json;
            result["status"] = "no_errors";
            response->success = true;
            response->message = result.dump();
            RCLCPP_INFO(this->get_logger(), "No servos have hardware errors");
            return;
        }

        // Reboot only the errored servos
        for (int servo_id : error_servo_ids) {
            RCLCPP_INFO(this->get_logger(), "Rebooting servo %d...", servo_id);
            dynamixel_->reboot(servo_id);
        }

        // Wait for servos to come back online after reboot
        RCLCPP_INFO(this->get_logger(), "Waiting for rebooted servos to come back online...");
        std::this_thread::sleep_for(std::chrono::milliseconds(1500));

        // Reconfigure and torque-on only the rebooted servos
        for (int servo_id : error_servo_ids) {
            RCLCPP_INFO(this->get_logger(), "Reconfiguring servo %d...", servo_id);
            configureServoByIdLocked(servo_id, true);
        }

        // Build JSON response with error IDs and status
        json result;
        result["error_ids"] = error_ids_json;
        result["status"] = "fixed";

        response->success = true;
        response->message = result.dump();
        RCLCPP_INFO(this->get_logger(), "Fixed %zu servo(s): %s",
            error_servo_ids.size(), response->message.c_str());
    } catch (const std::exception& e) {
        response->success = false;
        json err_result;
        err_result["error_ids"] = json::array();
        err_result["status"] = std::string("error: ") + e.what();
        response->message = err_result.dump();
        RCLCPP_ERROR(this->get_logger(), "Failed to fix error: %s", e.what());
    }
}

// ========== HEAD CONTROL ==========

int MauriceArmNode::logicalAngleToEncoder(double logical_angle_deg) {
    const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
    double angle_deg = head_config.head_direction_reversed ? -logical_angle_deg : logical_angle_deg;
    double angle_rad = angle_deg * M_PI / 180.0;
    int encoder_value = static_cast<int>((angle_rad / (2 * M_PI)) * 4096 + 2048);
    return encoder_value;
}

double MauriceArmNode::encoderToLogicalAngle(int encoder_value) {
    const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
    double angle_rad = (encoder_value - 2048) * (2 * M_PI) / 4096.0;
    double servo_angle_deg = angle_rad * 180.0 / M_PI;
    double logical_angle = head_config.head_direction_reversed ? -servo_angle_deg : servo_angle_deg;
    return logical_angle;
}

void MauriceArmNode::moveHeadToAngle(double logical_angle_deg) {
    std::lock_guard<std::mutex> lock(dynamixel_mutex_);
    moveHeadToAngleLocked(logical_angle_deg);
}

void MauriceArmNode::moveHeadToAngleLocked(double logical_angle_deg) {
    int encoder_value = logicalAngleToEncoder(logical_angle_deg);
    dynamixel_->setGoalPosition(7, encoder_value);
}

void MauriceArmNode::publishHeadPosition(int encoder_value) {
    double logical_angle = encoderToLogicalAngle(encoder_value);

    const auto& head_config = joint_configs_[6];  // Index 6 = joint 7

    json position_data;
    position_data["current_position"] = logical_angle;
    position_data["min_angle"] = head_config.head_min_angle_deg;
    position_data["max_angle"] = head_config.head_max_angle_deg;
    position_data["default_angle"] = 0.0;

    auto msg = std_msgs::msg::String();
    msg.data = position_data.dump();
    head_position_pub_->publish(msg);
}

void MauriceArmNode::headPositionCallback(const std_msgs::msg::Int32::SharedPtr msg) {
    try {
        double logical_position = static_cast<double>(msg->data);

        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7

        if (logical_position < head_config.head_min_angle_deg ||
            logical_position > head_config.head_max_angle_deg) {
            RCLCPP_ERROR(this->get_logger(), "Head position %f out of range [%f, %f]",
                logical_position, head_config.head_min_angle_deg, head_config.head_max_angle_deg);
            return;
        }

        int head_goal_encoder = logicalAngleToEncoder(logical_position);

        std::lock_guard<std::mutex> lock(head_command_mutex_);
        latest_head_command_ = head_goal_encoder;
        has_head_command_ = true;

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error in head position callback: %s", e.what());
    }
}

void MauriceArmNode::headAiPositionCallback(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    try {
        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7

        RCLCPP_INFO(this->get_logger(), "Moving head to AI position (%f deg)",
            head_config.head_ai_position_deg);

        int head_goal_encoder = logicalAngleToEncoder(head_config.head_ai_position_deg);

        std::lock_guard<std::mutex> lock(head_command_mutex_);
        latest_head_command_ = head_goal_encoder;
        has_head_command_ = true;

        response->success = true;
        response->message = "Head moving to AI position";

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error in head AI position callback: %s", e.what());
        response->success = false;
        response->message = e.what();
    }
}

void MauriceArmNode::headEnableServoCallback(
    const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
    std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/head/enable_servo (enable=%s)", request->data ? "true" : "false");
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        if (request->data) {
            RCLCPP_INFO(this->get_logger(), "  Enabling torque on head servo (ID 7)");
            dynamixel_->enableTorque(7);
            response->message = "Head servo enabled";
            RCLCPP_INFO(this->get_logger(), "Head servo enabled");
        } else {
            RCLCPP_INFO(this->get_logger(), "  Disabling torque on head servo (ID 7)");
            dynamixel_->disableTorque(7);
            response->message = "Head servo disabled";
            RCLCPP_INFO(this->get_logger(), "Head servo disabled");
        }
        response->success = true;
    } catch (const std::exception& e) {
        response->success = false;
        response->message = std::string("Failed: ") + e.what();
        RCLCPP_ERROR(this->get_logger(), "Failed to %s head servo: %s",
            request->data ? "enable" : "disable", e.what());
    }
}

} // namespace maurice_arm
