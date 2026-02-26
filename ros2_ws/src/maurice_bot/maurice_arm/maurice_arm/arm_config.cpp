// arm_config.cpp — Joint configuration loading and PID hot-reload
#include "maurice_arm/arm_node.hpp"

namespace maurice_arm {

void MauriceArmNode::loadJointConfigs(const std::vector<std::string>& joint_names) {
    RCLCPP_INFO(this->get_logger(), "Loading %zu joint configurations...", joint_names.size());

    for (size_t i = 0; i < joint_names.size(); ++i) {
        const std::string& jn = joint_names[i];  // e.g. "joint_1"
        JointConfig config;

        // Declare and read sub-parameters (nav2 style: joint_N.param)
        this->declare_parameter(jn + ".servo_id", 0);
        this->declare_parameter(jn + ".position_limits", std::vector<double>{});
        this->declare_parameter(jn + ".pwm_limit", 885);
        this->declare_parameter(jn + ".control_mode", 3);
        this->declare_parameter(jn + ".motor_type", std::string(""));
        this->declare_parameter(jn + ".current_limit", 0);
        this->declare_parameter(jn + ".homing_offset", 0);
        this->declare_parameter(jn + ".profile_velocity", 0);
        this->declare_parameter(jn + ".profile_acceleration", 0);
        this->declare_parameter(jn + ".gains_near", std::vector<int64_t>{});
        this->declare_parameter(jn + ".gains_far", std::vector<int64_t>{});
        this->declare_parameter(jn + ".gains_teleop", std::vector<int64_t>{});

        config.servo_id = static_cast<int>(this->get_parameter(jn + ".servo_id").as_int());
        auto pos_limits = this->get_parameter(jn + ".position_limits").as_double_array();
        if (pos_limits.size() >= 2) {
            config.min_pos_rad = pos_limits[0];
            config.max_pos_rad = pos_limits[1];
        }
        config.pwm_limit = static_cast<int>(this->get_parameter(jn + ".pwm_limit").as_int());
        config.control_mode = static_cast<int>(this->get_parameter(jn + ".control_mode").as_int());
        config.motor_type = this->get_parameter(jn + ".motor_type").as_string();

        // Validate motor_type vs control_mode
        if (!config.motor_type.empty() && !isX330(config.motor_type) &&
            (config.control_mode == 0 || config.control_mode == 5)) {
            throw std::runtime_error(
                jn + " (" + config.motor_type +
                "): control_mode " + std::to_string(config.control_mode) +
                " requires current hw — only x330 motors support modes 0/5");
        }

        // Current limit: x330 defaults to hw max, x430 has no current hw
        int cl = static_cast<int>(this->get_parameter(jn + ".current_limit").as_int());
        if (isX330(config.motor_type)) {
            config.current_limit = (cl > 0) ? cl : kX330MaxCurrentLimit;
            if (config.current_limit > kX330MaxCurrentLimit) {
                throw std::runtime_error(jn + ": current_limit out of range [1, " +
                    std::to_string(kX330MaxCurrentLimit) + "]");
            }
        } else if (cl > 0) {
            throw std::runtime_error(jn + " (" + config.motor_type +
                "): current_limit not supported — no current control hw");
        }

        config.homing_offset = static_cast<int>(this->get_parameter(jn + ".homing_offset").as_int());
        config.profile_velocity = static_cast<int>(this->get_parameter(jn + ".profile_velocity").as_int());
        config.profile_acceleration = static_cast<int>(this->get_parameter(jn + ".profile_acceleration").as_int());

        RCLCPP_INFO(this->get_logger(), "Joint %zu (%s): servo_id=%d, motor=%s, limits=[%.3f, %.3f] rad, pwm=%d, mode=%d, current=%d",
            i + 1, jn.c_str(), config.servo_id,
            config.motor_type.empty() ? "(unset)" : config.motor_type.c_str(),
            config.min_pos_rad, config.max_pos_rad, config.pwm_limit, config.control_mode, config.current_limit);
        if (config.homing_offset != 0)
            RCLCPP_INFO(this->get_logger(), "  Homing offset: %d", config.homing_offset);

        // Gains: gains_near = [kp, ki, kd, ff1, ff2], gains_far = [] means same as near
        auto near_arr = this->get_parameter(jn + ".gains_near").as_integer_array();
        GainProfile near_gains = parseGainsArray(near_arr);

        GainProfile far_gains = near_gains;  // default: far == near (gain scheduling no-ops)
        try {
            auto far_arr = this->get_parameter(jn + ".gains_far").as_integer_array();
            if (!far_arr.empty()) {
                far_gains = parseGainsArray(far_arr);
            }
        } catch (...) {}  // empty [] in YAML may not parse as integer array

        GainProfile teleop_gains{};  // teleop gains (November tuning)
        bool has_teleop_gains = false;
        try {
            auto teleop_arr = this->get_parameter(jn + ".gains_teleop").as_integer_array();
            if (!teleop_arr.empty()) {
                teleop_gains = parseGainsArray(teleop_arr);
                has_teleop_gains = true;
            }
        } catch (...) {}

        // Set joint config gains from near (initial operating gains)
        config.kp  = near_gains.kp;
        config.ki  = near_gains.ki;
        config.kd  = near_gains.kd;
        config.ff1 = near_gains.ff1;
        config.ff2 = near_gains.ff2;

        // Store gain scheduling profiles
        gs_near_[i] = near_gains;
        gs_far_[i]  = far_gains;
        gs_teleop_[i] = has_teleop_gains ? teleop_gains : near_gains;
        gs_last_applied_[i] = near_gains;

        RCLCPP_INFO(this->get_logger(), "  Gains near: [%d, %d, %d, %d, %d]  far: [%d, %d, %d, %d, %d]%s",
            near_gains.kp, near_gains.ki, near_gains.kd, near_gains.ff1, near_gains.ff2,
            far_gains.kp, far_gains.ki, far_gains.kd, far_gains.ff1, far_gains.ff2,
            (near_gains != far_gains) ? "  (gain scheduling active)" : "");
        if (has_teleop_gains)
            RCLCPP_INFO(this->get_logger(), "  Gains teleop: [%d, %d, %d, %d, %d]",
                teleop_gains.kp, teleop_gains.ki, teleop_gains.kd, teleop_gains.ff1, teleop_gains.ff2);
        if (config.profile_velocity > 0 || config.profile_acceleration > 0)
            RCLCPP_INFO(this->get_logger(), "  Profile: vel=%d, accel=%d", config.profile_velocity, config.profile_acceleration);

        // Head-specific config for joint 7 (index 6)
        if (i == 6) {
            this->declare_parameter(jn + ".head_config.ai_position_deg", 0.0);
            this->declare_parameter(jn + ".head_config.direction_reversed", false);

            config.head_ai_position_deg = this->get_parameter(jn + ".head_config.ai_position_deg").as_double();
            config.head_direction_reversed = this->get_parameter(jn + ".head_config.direction_reversed").as_bool();

            constexpr double RAD_TO_DEG = 180.0 / M_PI;
            if (config.head_direction_reversed) {
                config.head_min_angle_deg = -config.max_pos_rad * RAD_TO_DEG;
                config.head_max_angle_deg = -config.min_pos_rad * RAD_TO_DEG;
            } else {
                config.head_min_angle_deg = config.min_pos_rad * RAD_TO_DEG;
                config.head_max_angle_deg = config.max_pos_rad * RAD_TO_DEG;
            }

            RCLCPP_INFO(this->get_logger(), "  Head config: range=[%.1f, %.1f] deg, AI pos=%.1f deg, reversed=%s",
                config.head_min_angle_deg, config.head_max_angle_deg, config.head_ai_position_deg,
                config.head_direction_reversed ? "true" : "false");
        }

        joint_configs_.push_back(config);
    }
    RCLCPP_INFO(this->get_logger(), "Loaded %zu joint configurations", joint_configs_.size());
}

rcl_interfaces::msg::SetParametersResult MauriceArmNode::onParameterChange(
    const std::vector<rclcpp::Parameter>& parameters) {

    rcl_interfaces::msg::SetParametersResult result;
    result.successful = true;

    // Collect which joints had PID or profile changes
    std::set<int> pid_changed_joints;
    std::set<int> profile_changed_joints;

    for (const auto& param : parameters) {
        const std::string& name = param.get_name();

        // max_jerk is read on-the-fly each trajectory — just log it
        if (name == "max_jerk") {
            RCLCPP_INFO(this->get_logger(), "Hot-reload: max_jerk = %.1f rad/s³", param.as_double());
            continue;
        }

        // Match pattern: joint_N.<suffix>
        if (name.size() >= 8 && name.substr(0, 6) == "joint_") {
            size_t dot = name.find('.', 6);
            if (dot == std::string::npos) continue;

            int joint_num = 0;
            try {
                joint_num = std::stoi(name.substr(6, dot - 6));
            } catch (...) { continue; }

            if (joint_num < 1 || joint_num > static_cast<int>(joint_configs_.size())) continue;
            int ji = joint_num - 1;

            std::string suffix = name.substr(dot + 1);

            if (suffix == "gains_near") {
                auto arr = param.as_integer_array();
                GainProfile g = parseGainsArray(arr);
                gs_near_[ji] = g;
                joint_configs_[ji].kp  = g.kp;
                joint_configs_[ji].ki  = g.ki;
                joint_configs_[ji].kd  = g.kd;
                joint_configs_[ji].ff1 = g.ff1;
                joint_configs_[ji].ff2 = g.ff2;
                pid_changed_joints.insert(joint_num);
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_near = [%d, %d, %d, %d, %d]",
                    joint_num, g.kp, g.ki, g.kd, g.ff1, g.ff2);
            } else if (suffix == "gains_far") {
                try {
                    auto arr = param.as_integer_array();
                    gs_far_[ji] = arr.empty() ? gs_near_[ji] : parseGainsArray(arr);
                } catch (...) {
                    gs_far_[ji] = gs_near_[ji];
                }
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_far = [%d, %d, %d, %d, %d]",
                    joint_num, gs_far_[ji].kp, gs_far_[ji].ki, gs_far_[ji].kd, gs_far_[ji].ff1, gs_far_[ji].ff2);
            } else if (suffix == "gains_teleop") {
                try {
                    auto arr = param.as_integer_array();
                    gs_teleop_[ji] = arr.empty() ? gs_near_[ji] : parseGainsArray(arr);
                } catch (...) {
                    gs_teleop_[ji] = gs_near_[ji];
                }
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_teleop = [%d, %d, %d, %d, %d]",
                    joint_num, gs_teleop_[ji].kp, gs_teleop_[ji].ki, gs_teleop_[ji].kd, gs_teleop_[ji].ff1, gs_teleop_[ji].ff2);
            } else if (suffix == "profile_velocity") {
                joint_configs_[ji].profile_velocity = static_cast<int>(param.as_int());
                profile_changed_joints.insert(joint_num);
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.profile_velocity = %d",
                    joint_num, joint_configs_[ji].profile_velocity);
            } else if (suffix == "profile_acceleration") {
                joint_configs_[ji].profile_acceleration = static_cast<int>(param.as_int());
                profile_changed_joints.insert(joint_num);
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.profile_acceleration = %d",
                    joint_num, joint_configs_[ji].profile_acceleration);
            }
        }
    }

    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);

        if (!pid_changed_joints.empty()) {
            std::vector<std::tuple<int, int, int, int, int, int>> pid_data;
            for (int jn : pid_changed_joints) {
                const auto& c = joint_configs_[jn - 1];
                pid_data.emplace_back(c.servo_id, c.ff2, c.ff1, c.kd, c.ki, c.kp);
            }
            dynamixel_->syncWritePID(pid_data);
            RCLCPP_INFO(this->get_logger(), "Hot-reload: sync-wrote PID for %zu servo(s)", pid_data.size());
        }

        if (!profile_changed_joints.empty()) {
            std::vector<std::tuple<int, int, int>> profile_data;
            for (int jn : profile_changed_joints) {
                const auto& c = joint_configs_[jn - 1];
                profile_data.emplace_back(c.servo_id, c.profile_acceleration, c.profile_velocity);
            }
            dynamixel_->syncWriteProfile(profile_data);
            RCLCPP_INFO(this->get_logger(), "Hot-reload: sync-wrote profile for %zu servo(s)", profile_data.size());
        }

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Hot-reload syncWrite failed: %s", e.what());
        result.successful = false;
        result.reason = std::string("SyncWrite failed: ") + e.what();
    }

    return result;
}

} // namespace maurice_arm
