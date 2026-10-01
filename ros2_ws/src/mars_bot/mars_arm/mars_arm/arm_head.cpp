// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// arm_head.cpp — Head servo (ID 7): angle conversion, position topic and services
#include "mars_arm/arm_node.hpp"

using json = nlohmann::json;

namespace mars_arm {

int MarsArmNode::logicalAngleToEncoder(double logical_angle_deg) {
    const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
    double angle_deg = head_config.head_direction_reversed ? -logical_angle_deg : logical_angle_deg;
    double angle_rad = angle_deg * M_PI / 180.0;
    int encoder_value = static_cast<int>((angle_rad / (2 * M_PI)) * 4096 + 2048);
    return encoder_value;
}

double MarsArmNode::encoderToLogicalAngle(int encoder_value) {
    const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
    double angle_rad = (encoder_value - 2048) * (2 * M_PI) / 4096.0;
    double servo_angle_deg = angle_rad * 180.0 / M_PI;
    double logical_angle = head_config.head_direction_reversed ? -servo_angle_deg : servo_angle_deg;
    return logical_angle;
}

void MarsArmNode::commandHead(double logical_angle_deg) {
    std::lock_guard<std::mutex> lock(head_command_mutex_);
    latest_head_command_ = logicalAngleToEncoder(logical_angle_deg);
    has_head_command_ = true;
}

void MarsArmNode::publishHeadPosition(int encoder_value) {
    // On change (two-tick deadband, at most 20 Hz) plus a 1 Hz keepalive: the
    // value only moves on a head command, and every subscriber parses JSON.
    const auto now = std::chrono::steady_clock::now();
    const auto since_last = now - last_head_publish_time_;
    const bool moved = std::abs(encoder_value - last_published_head_encoder_) >= 2;
    if (since_last < std::chrono::seconds(1) && (!moved || since_last < std::chrono::milliseconds(50))) {
        return;
    }
    last_published_head_encoder_ = encoder_value;
    last_head_publish_time_ = now;
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

void MarsArmNode::headPositionCallback(const std_msgs::msg::Int32::SharedPtr msg) {
    try {
        double logical_position = static_cast<double>(msg->data);

        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7

        if (logical_position < head_config.head_min_angle_deg || logical_position > head_config.head_max_angle_deg) {
            RCLCPP_ERROR(this->get_logger(), "Head position %f out of range [%f, %f]", logical_position,
                         head_config.head_min_angle_deg, head_config.head_max_angle_deg);
            return;
        }

        commandHead(logical_position);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error in head position callback: %s", e.what());
    }
}

void MarsArmNode::headAiPositionCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
                                         std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    try {
        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7

        RCLCPP_INFO(this->get_logger(), "Moving head to AI position (%f deg)", head_config.head_ai_position_deg);
        commandHead(head_config.head_ai_position_deg);

        response->success = true;
        response->message = "Head moving to AI position";

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error in head AI position callback: %s", e.what());
        response->success = false;
        response->message = e.what();
    }
}

void MarsArmNode::headEnableServoCallback(const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
                                          std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/head/enable_servo (enable=%s)",
                request->data ? "true" : "false");
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
        RCLCPP_ERROR(this->get_logger(), "Failed to %s head servo: %s", request->data ? "enable" : "disable", e.what());
    }
}

}  // namespace mars_arm
