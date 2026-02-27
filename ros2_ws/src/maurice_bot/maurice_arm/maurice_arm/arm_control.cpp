// arm_control.cpp — Control loop, gain scheduling, timing, limit/encoder conversion
#include "maurice_arm/arm_node.hpp"

namespace maurice_arm {

void MauriceArmNode::controlTimerCallback() {
    // ts[0..8]: loop_start, lock_acquired, read_done, effort_done,
    //           pub_arm_done, pub_head_js_done, gain_sched_done, cmd_write_done, loop_end
    std::array<std::chrono::steady_clock::time_point, 9> ts;
    ts[0] = std::chrono::steady_clock::now();
    ts.fill(ts[0]);
    try {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);
        ts[1] = std::chrono::steady_clock::now();

        // ========== READ STATE (position + velocity + load in one bulk read) ==========
        auto [positions, velocities, loads] = robot_->readState();
        ts[2] = std::chrono::steady_clock::now();

        // Convert raw load/current to effort values
        std::vector<double> efforts;
        for (size_t j = 0; j < loads.size() && j < joint_configs_.size(); ++j) {
            if (joint_configs_[j].current_limit > 0) {
                efforts.push_back(static_cast<double>(loads[j]));        // mA
            } else {
                efforts.push_back(static_cast<double>(loads[j]) / 10.0); // %
            }
        }
        ts[3] = std::chrono::steady_clock::now();

        // ========== MOTOR STRESS PROTECTION ==========
        double dt = 1.0 / control_frequency_;
        if (stress_enabled_) {
            updateMotorStress(dt, loads);
        }

        // ========== PUBLISH ARM STATE ==========
        std::vector<double> positions_rad;
        for (int pos : positions) {
            positions_rad.push_back(((pos - 2048) * 2 * M_PI) / 4096.0);
        }

        std::vector<double> velocities_rad;
        for (int vel : velocities) {
            velocities_rad.push_back((vel * 2 * M_PI) / 4096.0);
        }

        // Flip directions for joints 2, 3, 4, 6 (indices 1, 2, 3, 5)
        std::array<size_t, 4> flip_indices = {1, 2, 3, 5};
        for (size_t idx : flip_indices) {
            if (idx < positions_rad.size()) {
                positions_rad[idx] = -positions_rad[idx];
                velocities_rad[idx] = -velocities_rad[idx];
            }
        }

        // Publish arm joint state (only first 6 servos) to /mars/arm/state
        arm_state_msg_.header.stamp = this->now();
        arm_state_msg_.position = std::vector<double>(positions_rad.begin(), positions_rad.begin() + 6);
        arm_state_msg_.velocity = std::vector<double>(velocities_rad.begin(), velocities_rad.begin() + 6);
        arm_state_msg_.effort = std::vector<double>(efforts.begin(), efforts.begin() + 6);
        arm_state_pub_->publish(arm_state_msg_);

        // Store latest joint positions for trajectory planning
        {
            std::lock_guard<std::mutex> js_lock(joint_state_mutex_);
            latest_joint_positions_ = arm_state_msg_.position;
        }
        ts[4] = std::chrono::steady_clock::now();

        // ========== PUBLISH HEAD POSITION ==========
        int head_encoder = positions[6];
        publishHeadPosition(head_encoder);

        // ========== PUBLISH FULL JOINT STATE (all 7 joints) to /joint_states ==========
        double head_angle_rad = positions_rad[6];
        if (joint_configs_[6].head_direction_reversed) {
            head_angle_rad = -head_angle_rad;
        }

        joint_state_msg_.header.stamp = this->now();
        std::vector<double> all_positions(positions_rad.begin(), positions_rad.begin() + 6);
        all_positions.push_back(head_angle_rad);
        joint_state_msg_.position = all_positions;
        std::vector<double> all_velocities(velocities_rad.begin(), velocities_rad.begin() + 6);
        all_velocities.push_back(velocities_rad[6]);
        joint_state_msg_.velocity = all_velocities;
        std::vector<double> all_efforts(efforts.begin(), efforts.begin() + 6);
        all_efforts.push_back(efforts[6]);
        joint_state_msg_.effort = all_efforts;
        joint_state_pub_->publish(joint_state_msg_);
        ts[5] = std::chrono::steady_clock::now();

        // ========== PERIODIC GAIN DUMP (every 2s) ==========
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
            "Gains: J1[P=%d I=%d D=%d F1=%d F2=%d] J2[P=%d I=%d D=%d F1=%d F2=%d] "
            "J3[P=%d I=%d D=%d F1=%d F2=%d] J4[P=%d I=%d D=%d F1=%d F2=%d] "
            "J5[P=%d I=%d D=%d F1=%d F2=%d] J6[P=%d I=%d D=%d F1=%d F2=%d] "
            "J7[P=%d I=%d D=%d F1=%d F2=%d]",
            joint_configs_[0].kp, joint_configs_[0].ki, joint_configs_[0].kd, joint_configs_[0].ff1, joint_configs_[0].ff2,
            joint_configs_[1].kp, joint_configs_[1].ki, joint_configs_[1].kd, joint_configs_[1].ff1, joint_configs_[1].ff2,
            joint_configs_[2].kp, joint_configs_[2].ki, joint_configs_[2].kd, joint_configs_[2].ff1, joint_configs_[2].ff2,
            joint_configs_[3].kp, joint_configs_[3].ki, joint_configs_[3].kd, joint_configs_[3].ff1, joint_configs_[3].ff2,
            joint_configs_[4].kp, joint_configs_[4].ki, joint_configs_[4].kd, joint_configs_[4].ff1, joint_configs_[4].ff2,
            joint_configs_[5].kp, joint_configs_[5].ki, joint_configs_[5].kd, joint_configs_[5].ff1, joint_configs_[5].ff2,
            joint_configs_[6].kp, joint_configs_[6].ki, joint_configs_[6].kd, joint_configs_[6].ff1, joint_configs_[6].ff2);

        // ========== GAIN SCHEDULING ==========
        // TELEOP mode: use flat teleop gains (no extension-based interpolation)
        // SCHEDULED mode: interpolate near/far by arm extension (gravity compensation)
        if (++gs_cycle_counter_ >= kGainScheduleInterval) {
            gs_cycle_counter_ = 0;

            bool gs_changed = false;
            std::vector<std::tuple<int, int, int, int, int, int>> gs_pid_data;

            if (gain_mode_ == GainMode::TELEOP) {
                // On transition to TELEOP: zero out profile limits for instant response
                if (last_applied_gain_mode_ != GainMode::TELEOP) {
                    last_applied_gain_mode_ = GainMode::TELEOP;
                    for (int i = 0; i < 6; i++) {
                        dynamixel_->setProfileVelocity(joint_configs_[i].servo_id, 0);
                        dynamixel_->setProfileAcceleration(joint_configs_[i].servo_id, 0);
                    }
                    RCLCPP_INFO(this->get_logger(), "TELEOP: profile velocity/acceleration set to 0 (unlimited) for joints 1-6");
                }

                // TELEOP: apply flat teleop gains for joints 1-6
                for (int i = 0; i < 6; i++) {
                    const GainProfile& target = gs_teleop_[i];
                    if (target != gs_last_applied_[i]) {
                        gs_changed = true;
                        gs_last_applied_[i] = target;
                        joint_configs_[i].kp  = target.kp;
                        joint_configs_[i].ki  = target.ki;
                        joint_configs_[i].kd  = target.kd;
                        joint_configs_[i].ff1 = target.ff1;
                        joint_configs_[i].ff2 = target.ff2;
                    }
                    gs_pid_data.emplace_back(joint_configs_[i].servo_id, target.ff2, target.ff1, target.kd, target.ki, target.kp);
                }
                if (gs_changed) {
                    auto pid_start = std::chrono::steady_clock::now();
                    dynamixel_->syncWritePID(gs_pid_data);
                    auto pid_us = std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - pid_start).count();
                    timing_stats_[8].add(pid_us);
                    RCLCPP_INFO(this->get_logger(),
                        "TELEOP gains applied | J1 P=%d I=%d D=%d | J2 P=%d I=%d D=%d | J3 P=%d I=%d D=%d | J4 P=%d I=%d D=%d",
                        gs_teleop_[0].kp, gs_teleop_[0].ki, gs_teleop_[0].kd,
                        gs_teleop_[1].kp, gs_teleop_[1].ki, gs_teleop_[1].kd,
                        gs_teleop_[2].kp, gs_teleop_[2].ki, gs_teleop_[2].kd,
                        gs_teleop_[3].kp, gs_teleop_[3].ki, gs_teleop_[3].kd);
                }
            } else {
                // On transition to SCHEDULED: restore profile limits from config
                if (last_applied_gain_mode_ != GainMode::SCHEDULED) {
                    last_applied_gain_mode_ = GainMode::SCHEDULED;
                    for (int i = 0; i < 6; i++) {
                        if (joint_configs_[i].profile_velocity > 0)
                            dynamixel_->setProfileVelocity(joint_configs_[i].servo_id, joint_configs_[i].profile_velocity);
                        if (joint_configs_[i].profile_acceleration > 0)
                            dynamixel_->setProfileAcceleration(joint_configs_[i].servo_id, joint_configs_[i].profile_acceleration);
                    }
                    RCLCPP_INFO(this->get_logger(), "SCHEDULED: profile velocity/acceleration restored from config");
                }

                // SCHEDULED: interpolate near/far by arm extension for joints 1-4
                constexpr double L2_x = 0.02825, L2_z = 0.12125;
                constexpr double L3_x = 0.1375,  L3_z = 0.0045;
                constexpr double L45_x = 0.110838;
                constexpr double kMaxReach = 0.37291;

                double q2 = positions_rad[1], q3 = positions_rad[2], q4 = positions_rad[3];
                double a2 = q2, a23 = q2 + q3, a234 = q2 + q3 + q4;

                double ee_x = L2_x*std::cos(a2)  + L2_z*std::sin(a2)
                            + L3_x*std::cos(a23) + L3_z*std::sin(a23)
                            + L45_x*std::cos(a234);

                double horiz_reach = std::abs(ee_x);
                double extension_linear = std::clamp((horiz_reach / kMaxReach - 0.1) / 0.9, 0.0, 1.0);
                double extension = extension_linear * extension_linear;

                for (int i = 0; i < 4; i++) {
                    GainProfile interp;
                    interp.kp = gs_near_[i].kp + static_cast<int>(extension * (gs_far_[i].kp - gs_near_[i].kp));
                    interp.ki = gs_near_[i].ki + static_cast<int>(extension * (gs_far_[i].ki - gs_near_[i].ki));
                    interp.kd = gs_near_[i].kd + static_cast<int>(extension * (gs_far_[i].kd - gs_near_[i].kd));
                    interp.ff1 = gs_near_[i].ff1 + static_cast<int>(extension * (gs_far_[i].ff1 - gs_near_[i].ff1));
                    interp.ff2 = gs_near_[i].ff2 + static_cast<int>(extension * (gs_far_[i].ff2 - gs_near_[i].ff2));

                    if (interp != gs_last_applied_[i]) {
                        gs_changed = true;
                        gs_last_applied_[i] = interp;
                        joint_configs_[i].kp  = interp.kp;
                        joint_configs_[i].ki  = interp.ki;
                        joint_configs_[i].kd  = interp.kd;
                        joint_configs_[i].ff1 = interp.ff1;
                        joint_configs_[i].ff2 = interp.ff2;
                    }
                    gs_pid_data.emplace_back(joint_configs_[i].servo_id, interp.ff2, interp.ff1, interp.kd, interp.ki, interp.kp);
                }
                if (gs_changed) {
                    auto pid_start = std::chrono::steady_clock::now();
                    dynamixel_->syncWritePID(gs_pid_data);
                    auto pid_us = std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - pid_start).count();
                    timing_stats_[8].add(pid_us);
                    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                        "GainSched ext=%.2f (h=%.3fm) | J1 P=%d D=%d | J2 P=%d D=%d | J3 P=%d D=%d | J4 P=%d D=%d",
                        extension, horiz_reach,
                        gs_last_applied_[0].kp, gs_last_applied_[0].kd,
                        gs_last_applied_[1].kp, gs_last_applied_[1].kd,
                        gs_last_applied_[2].kp, gs_last_applied_[2].kd,
                        gs_last_applied_[3].kp, gs_last_applied_[3].kd);
                }
            }
        }
        ts[6] = std::chrono::steady_clock::now();

        // ========== COMMAND PASS-THROUGH (runs at 200Hz) ==========
        {
            bool has_cmd = false;
            std::array<double, 6> result_rad{};

            {
                std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
                if (has_target_) {
                    result_rad = latest_target_;
                    has_cmd = true;
                }
            }

            if (has_cmd) {
                std::vector<double> cmd_vec(result_rad.begin(), result_rad.end());
                std::vector<int> arm_encoder = applyLimitsAndConvertToEncoder(cmd_vec);

                std::vector<int> full_command(7);
                std::copy(arm_encoder.begin(), arm_encoder.end(), full_command.begin());
                {
                    std::lock_guard<std::mutex> head_lock(head_command_mutex_);
                    full_command[6] = latest_head_command_;
                    has_head_command_ = false;
                }

                robot_->setGoalPos(full_command);

                // Publish command (in radians, external convention)
                sensor_msgs::msg::JointState cmd_msg;
                cmd_msg.header.stamp = this->now();
                cmd_msg.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
                cmd_msg.position.resize(6);
                for (int i = 0; i < 6; ++i) {
                    double rad = ((full_command[i] - 2048) * 2 * M_PI) / 4096.0;
                    if (i == 1 || i == 2 || i == 3 || i == 5) rad = -rad;
                    cmd_msg.position[i] = rad;
                }
                arm_command_state_pub_->publish(cmd_msg);
            } else if (has_head_command_.load()) {
                std::lock_guard<std::mutex> head_lock(head_command_mutex_);
                int head_enc = latest_head_command_;
                has_head_command_ = false;
                std::vector<int> full_command(latest_arm_command_);
                full_command.resize(7);
                full_command[6] = head_enc;
                robot_->setGoalPos(full_command);
            }
        }
        ts[7] = std::chrono::steady_clock::now();

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Control timer error: %s", e.what());
    }

    ts[8] = std::chrono::steady_clock::now();
    recordLoopTiming(ts);
}

void MauriceArmNode::recordLoopTiming(std::array<std::chrono::steady_clock::time_point, 9>& ts) {
    for (size_t i = 1; i < ts.size(); ++i)
        if (ts[i] < ts[i - 1]) ts[i] = ts[i - 1];

    std::array<long, 10> us{};
    us[0] = std::chrono::duration_cast<std::chrono::microseconds>(ts[8] - ts[0]).count();
    for (size_t i = 1; i <= 7; ++i)
        us[i] = std::chrono::duration_cast<std::chrono::microseconds>(ts[i] - ts[i - 1]).count();
    us[9] = std::chrono::duration_cast<std::chrono::microseconds>(ts[8] - ts[7]).count();

    for (size_t i = 0; i < timing_stats_.size(); ++i)
        if (i != 8) timing_stats_[i].add(us[i]);

    long avg_total = timing_stats_[0].avg();

    std::ostringstream msg;
    msg << "LoopTiming(us avg|max, n=" << timing_stats_[0].samples << "):";
    for (size_t i = 0; i < timing_stats_.size(); ++i)
        msg << ' ' << timing_stats_[i].name << '=' << timing_stats_[i].avg() << '|' << timing_stats_[i].max_us;
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "%s", msg.str().c_str());

    std::ostringstream pct;
    pct << std::fixed << std::setprecision(1) << "LoopTiming(%):";
    for (size_t i : {1u, 2u, 3u, 4u, 5u, 6u, 7u, 9u})
        pct << ' ' << timing_stats_[i].name << '='
            << (avg_total > 0 ? 100.0 * timing_stats_[i].avg() / avg_total : 0.0);
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "%s", pct.str().c_str());

    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
        "SerialBus(us): readState_txrx=%ld  write_txrx=%ld",
        robot_->last_read_txrx_us, robot_->last_write_txrx_us);
}

std::vector<int> MauriceArmNode::applyLimitsAndConvertToEncoder(std::vector<double>& command_data) {
    // ===== INTELLIGENT JOINT LIMITS =====
    if (command_data.size() >= 2) {
        double joint1_pos = command_data[0];
        double joint2_pos = command_data[1];

        const auto& joint2_config = joint_configs_[1];
        double config_min = joint2_config.min_pos_rad;
        double config_max = joint2_config.max_pos_rad;

        double joint2_min_limit = -config_max;
        double joint2_max_limit = -config_min;

        const double original_min_limit = -config_max;
        const double restricted_limit = -0.5;

        if (joint1_pos < -1.35) {
            // Negative side clear — no restriction
        } else if (joint1_pos < -1.0) {
            double t = - (joint1_pos - (-1.0)) / (-1.0 - (-1.35));
            double interpolated_limit = restricted_limit + t * (original_min_limit - restricted_limit);
            joint2_min_limit = std::max(joint2_min_limit, interpolated_limit);
        } else if (joint1_pos < 1.0) {
            joint2_min_limit = std::max(joint2_min_limit, restricted_limit);
        } else if (joint1_pos < 1.25) {
            double t = (joint1_pos - 1.0) / (1.25 - 1.0);
            double interpolated_limit = restricted_limit + t * (original_min_limit - restricted_limit);
            joint2_min_limit = std::max(joint2_min_limit, interpolated_limit);
        }

        if (joint2_pos < joint2_min_limit) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
                "Joint2 limited due to joint1=%.3f: requested %.3f, clamped to %.3f",
                joint1_pos, joint2_pos, joint2_min_limit);
        }

        command_data[1] = std::clamp(joint2_pos, joint2_min_limit, joint2_max_limit);
    }

    // Direction flips for joints 2, 3, 4, 6 (indices 1, 2, 3, 5)
    std::array<size_t, 4> flip_indices = {1, 2, 3, 5};
    for (size_t idx : flip_indices) {
        if (idx < command_data.size()) {
            command_data[idx] = -command_data[idx];
        }
    }

    // Convert to encoder counts
    std::vector<int> command_encoder;
    for (double pos : command_data) {
        command_encoder.push_back(static_cast<int>((pos / (2 * M_PI)) * 4096 + 2048));
    }
    return command_encoder;
}

void MauriceArmNode::updateMotorStress(double dt, const std::vector<int>& loads) {
    auto now = std::chrono::steady_clock::now();

    // Only track arm joints (0-5), not head (6)
    for (size_t j = 0; j < 6 && j < loads.size(); ++j) {
        auto& tracker = stress_trackers_[j];

        // If in cooldown, check if it's time to re-enable
        if (tracker.in_cooldown) {
            if (now >= tracker.cooldown_until) {
                tracker.in_cooldown = false;
                tracker.score = 0.0;

                // Re-enable torque on this servo
                int servo_id = joint_configs_[j].servo_id;
                RCLCPP_WARN(this->get_logger(),
                    "Stress cooldown ended for joint %zu (servo %d) — re-enabling torque",
                    j + 1, servo_id);
                try {
                    dynamixel_->enableTorque(servo_id);
                } catch (const std::exception& e) {
                    RCLCPP_ERROR(this->get_logger(),
                        "Failed to re-enable torque on servo %d after cooldown: %s",
                        servo_id, e.what());
                }
            }
            continue;  // skip scoring while in cooldown
        }

        // Leaky integrator: score += (A * |load| - C) * dt, clamped >= 0
        // loads[j]: mA for x330, 0.1% for x430
        double load_abs = std::abs(static_cast<double>(loads[j]));
        double stress_delta = (stress_multiplier_ * load_abs - stress_leak_) * dt;
        tracker.score = std::max(0.0, tracker.score + stress_delta);

        // Check threshold
        if (tracker.score >= stress_threshold_) {
            int servo_id = joint_configs_[j].servo_id;
            RCLCPP_WARN(this->get_logger(),
                "Stress threshold exceeded for joint %zu (servo %d): score=%.1f >= %.1f — disabling torque for %.1fs",
                j + 1, servo_id, tracker.score, stress_threshold_, stress_cooldown_sec_);

            tracker.in_cooldown = true;
            tracker.cooldown_until = now + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(stress_cooldown_sec_));

            try {
                dynamixel_->disableTorque(servo_id);
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(),
                    "Failed to disable torque on servo %d for stress cooldown: %s",
                    servo_id, e.what());
            }
        }
    }

    // Periodic log of stress scores
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
        "StressScores: J1=%.1f J2=%.1f J3=%.1f J4=%.1f J5=%.1f J6=%.1f (threshold=%.1f)",
        stress_trackers_[0].score, stress_trackers_[1].score,
        stress_trackers_[2].score, stress_trackers_[3].score,
        stress_trackers_[4].score, stress_trackers_[5].score,
        stress_threshold_);
}

} // namespace maurice_arm
