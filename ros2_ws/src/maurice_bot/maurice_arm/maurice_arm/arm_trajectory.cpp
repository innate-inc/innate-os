// arm_trajectory.cpp — Trajectory planning and execution
#include "maurice_arm/arm_node.hpp"

namespace maurice_arm {

// ========== CUBIC SPLINE (QUINTIC SMOOTHERSTEP) ==========

std::vector<std::vector<double>> MauriceArmNode::computeCubicSplineTrajectory(
    const std::vector<double>& start,
    const std::vector<double>& goal,
    double duration,
    double dt) {

    std::vector<std::vector<double>> trajectory;

    if (start.size() != goal.size() || start.empty()) {
        RCLCPP_ERROR(this->get_logger(), "Invalid start/goal sizes for spline trajectory");
        return trajectory;
    }

    // Jerk limiting: extend duration if needed so peak jerk stays within max_jerk.
    // Quintic smootherstep peak jerk = 60 * |Δθ| / T³  (at t=0 and t=T).
    double max_jerk = this->get_parameter("max_jerk").as_double();
    if (max_jerk > 0.0) {
        double max_delta = 0.0;
        for (size_t j = 0; j < start.size(); ++j) {
            max_delta = std::max(max_delta, std::abs(goal[j] - start[j]));
        }
        double min_duration = std::cbrt(60.0 * max_delta / max_jerk);
        if (min_duration > duration) {
            RCLCPP_INFO(this->get_logger(),
                "Jerk limit %.1f rad/s³: extending trajectory %.2fs → %.2fs (Δθ_max=%.3f rad)",
                max_jerk, duration, min_duration, max_delta);
            duration = min_duration;
        }
    }

    int num_steps = static_cast<int>(duration / dt);
    if (num_steps < 1) num_steps = 1;

    for (int step = 0; step <= num_steps; ++step) {
        double t = step * dt;
        double t_ratio = t / duration;
        // Quintic smootherstep: zero velocity + zero acceleration at endpoints
        double ratio = t_ratio * t_ratio * t_ratio * (t_ratio * (6.0 * t_ratio - 15.0) + 10.0);

        std::vector<double> point(start.size());
        for (size_t j = 0; j < start.size(); ++j) {
            point[j] = start[j] + (goal[j] - start[j]) * ratio;
        }
        trajectory.push_back(point);
    }

    return trajectory;
}

// ========== PLAN AND EXECUTE ==========

bool MauriceArmNode::planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time,
                                               GainMode trajectory_gain_mode) {
    // Switch gains for trajectory execution
    if (gain_mode_ != trajectory_gain_mode) {
        gain_mode_ = trajectory_gain_mode;
        RCLCPP_INFO(this->get_logger(), "Gain mode -> %s (trajectory execution)",
                    trajectory_gain_mode == GainMode::TELEOP ? "TELEOP" : "SCHEDULED");
    }

    // Validate inputs - 6 joints (arm + gripper)
    if (target_positions.size() != 6) {
        RCLCPP_ERROR(this->get_logger(), "Target must have 6 joint positions, got %zu", target_positions.size());
        return false;
    }

    if (trajectory_time <= 0.0) {
        RCLCPP_ERROR(this->get_logger(), "Trajectory time must be positive, got %.3f", trajectory_time);
        return false;
    }

    // Get current joint state (6 joints including gripper)
    std::vector<double> current_positions;
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        if (latest_joint_positions_.empty()) {
            RCLCPP_ERROR(this->get_logger(), "No current joint state available");
            return false;
        }
        current_positions = latest_joint_positions_;
    }

    if (current_positions.size() != 6) {
        RCLCPP_ERROR(this->get_logger(), "Current state has %zu joints, expected 6", current_positions.size());
        return false;
    }

    // Use simple cubic spline planning (fast, smooth trajectory)
    RCLCPP_INFO(this->get_logger(), "Planning with cubic spline for 6-DOF arm (including gripper)...");
    const double dt = 1.0 / this->get_parameter("trajectory_rate_hz").as_double();
    auto interpolated_trajectory = computeCubicSplineTrajectory(current_positions, target_positions, trajectory_time, dt);

    if (interpolated_trajectory.empty()) {
        RCLCPP_ERROR(this->get_logger(), "Cubic spline trajectory computation failed");
        return false;
    }

    // Detect if jerk limiting extended the duration
    double actual_duration = (interpolated_trajectory.size() - 1) * dt;
    if (actual_duration > trajectory_time * 1.01) {
        RCLCPP_WARN(this->get_logger(),
            "Jerk-limited: requested %.2fs but executing %.2fs (+%.0f%%)",
            trajectory_time, actual_duration,
            100.0 * (actual_duration - trajectory_time) / trajectory_time);
    }

    RCLCPP_INFO(this->get_logger(), "Executing trajectory with %zu waypoints over %.2f seconds",
                interpolated_trajectory.size(), actual_duration);

    // Execute trajectory by sending each waypoint with a sleep
    auto sleep_duration = std::chrono::duration<double>(dt);
    for (size_t i = 0; i < interpolated_trajectory.size(); ++i) {
        const auto& point = interpolated_trajectory[i];

        // Send command via the control loop's pass-through path
        {
            std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
            for (size_t j = 0; j < 6 && j < point.size(); ++j) {
                latest_target_[j] = point[j];
            }
            has_target_ = true;
        }

        // Sleep until next waypoint (except for last point)
        if (i < interpolated_trajectory.size() - 1) {
            std::this_thread::sleep_for(sleep_duration);
        }
    }

    RCLCPP_INFO(this->get_logger(), "Trajectory execution complete");
    return true;
}

bool MauriceArmNode::planAndExecuteMultiWaypointTrajectory(
    const std::vector<std::vector<double>>& waypoints,
    const std::vector<double>& segment_durations) {

    // Switch to scheduled gains for planned trajectories
    if (gain_mode_ != GainMode::SCHEDULED) {
        gain_mode_ = GainMode::SCHEDULED;
        RCLCPP_INFO(this->get_logger(), "Gain mode -> SCHEDULED (multi-waypoint trajectory)");
    }

    if (waypoints.size() < 2) {
        RCLCPP_ERROR(this->get_logger(), "Need at least 2 waypoints for trajectory");
        return false;
    }
    if (segment_durations.size() != waypoints.size() - 1) {
        RCLCPP_ERROR(this->get_logger(), "segment_durations size (%zu) must equal waypoints-1 (%zu)",
                    segment_durations.size(), waypoints.size() - 1);
        return false;
    }

    const double dt = 1.0 / this->get_parameter("trajectory_rate_hz").as_double();

    // Build one big trajectory by linearly interpolating each segment
    std::vector<std::vector<double>> full_trajectory;

    for (size_t seg = 0; seg < segment_durations.size(); ++seg) {
        double dur = segment_durations[seg];
        if (dur <= 0.0) {
            RCLCPP_WARN(this->get_logger(), "Segment %zu has non-positive duration %.3f, skipping", seg, dur);
            continue;
        }

        const auto& start = waypoints[seg];
        const auto& end = waypoints[seg + 1];
        int num_steps = std::max(1, static_cast<int>(dur / dt));

        // For all segments except the first, skip step 0 (already added as
        // last point of previous segment).
        int start_step = (seg == 0) ? 0 : 1;

        for (int step = start_step; step <= num_steps; ++step) {
            double alpha = static_cast<double>(step) / num_steps;
            std::vector<double> point(start.size());
            for (size_t j = 0; j < start.size(); ++j) {
                point[j] = start[j] + alpha * (end[j] - start[j]);
            }
            full_trajectory.push_back(point);
        }
    }

    if (full_trajectory.empty()) {
        RCLCPP_ERROR(this->get_logger(), "Multi-waypoint trajectory is empty after interpolation");
        return false;
    }

    RCLCPP_INFO(this->get_logger(), "Executing multi-waypoint trajectory: %zu segments, %zu total points",
                segment_durations.size(), full_trajectory.size());

    // Execute: send each point at dt intervals
    auto sleep_duration = std::chrono::duration<double>(dt);
    for (size_t i = 0; i < full_trajectory.size(); ++i) {
        const auto& point = full_trajectory[i];

        {
            std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
            for (size_t j = 0; j < 6 && j < point.size(); ++j) {
                latest_target_[j] = point[j];
            }
            has_target_ = true;
        }

        if (i < full_trajectory.size() - 1) {
            std::this_thread::sleep_for(sleep_duration);
        }
    }

    RCLCPP_INFO(this->get_logger(), "Multi-waypoint trajectory execution complete");
    return true;
}

// ========== SERVICE CALLBACKS ==========

void MauriceArmNode::armGotoJSTrajectoryCallback(
    const std::shared_ptr<maurice_msgs::srv::GotoJSTrajectory::Request> request,
    std::shared_ptr<maurice_msgs::srv::GotoJSTrajectory::Response> response) {

    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/goto_js_trajectory");

    int num_joints = request->num_joints;
    const auto& flat = request->waypoints.data;
    const auto& seg_durs = request->segment_durations;

    if (num_joints <= 0 || flat.size() % num_joints != 0) {
        RCLCPP_ERROR(this->get_logger(), "Invalid waypoints: %zu values not divisible by %d joints",
                    flat.size(), num_joints);
        response->success = false;
        return;
    }

    // Unpack flat array into waypoint vectors
    size_t num_waypoints = flat.size() / num_joints;
    std::vector<std::vector<double>> waypoints;
    for (size_t i = 0; i < num_waypoints; ++i) {
        waypoints.emplace_back(flat.begin() + i * num_joints,
                               flat.begin() + (i + 1) * num_joints);
    }

    std::vector<double> durations(seg_durs.begin(), seg_durs.end());

    RCLCPP_INFO(this->get_logger(), "Trajectory: %zu waypoints, %zu segments",
                waypoints.size(), durations.size());

    // Prepend current position as waypoint[0] so the arm starts from where it is
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        if (!latest_joint_positions_.empty()) {
            waypoints.insert(waypoints.begin(), latest_joint_positions_);
            if (!durations.empty()) {
                durations.insert(durations.begin(), durations[0]);
            } else {
                durations.insert(durations.begin(), 0.5);
            }
        }
    }

    response->success = planAndExecuteMultiWaypointTrajectory(waypoints, durations);
}

void MauriceArmNode::armGotoJSCallback(
    const std::shared_ptr<maurice_msgs::srv::GotoJS::Request> request,
    std::shared_ptr<maurice_msgs::srv::GotoJS::Response> response) {

    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/goto_js (TELEOP gains)");

    // Extract target positions and time from request
    std::vector<double> target_positions(request->data.data.begin(), request->data.data.end());
    double trajectory_time = request->time;

    RCLCPP_INFO(this->get_logger(), "Target (6 DOF): [%.3f, %.3f, %.3f, %.3f, %.3f, %.3f], Time: %.2fs",
                target_positions.size() > 0 ? target_positions[0] : 0.0,
                target_positions.size() > 1 ? target_positions[1] : 0.0,
                target_positions.size() > 2 ? target_positions[2] : 0.0,
                target_positions.size() > 3 ? target_positions[3] : 0.0,
                target_positions.size() > 4 ? target_positions[4] : 0.0,
                target_positions.size() > 5 ? target_positions[5] : 0.0,
                trajectory_time);

    // goto_js uses teleop gains (flat, no extension-based interpolation)
    response->success = planAndExecuteTrajectory(target_positions, trajectory_time, GainMode::TELEOP);

    if (response->success) {
        RCLCPP_INFO(this->get_logger(), "Successfully planned trajectory");
    } else {
        RCLCPP_ERROR(this->get_logger(), "Failed to plan trajectory");
    }
}

void MauriceArmNode::armGotoJSV2Callback(
    const std::shared_ptr<maurice_msgs::srv::GotoJS::Request> request,
    std::shared_ptr<maurice_msgs::srv::GotoJS::Response> response) {

    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/goto_js_v2 (SCHEDULED gains)");

    // Extract target positions and time from request
    std::vector<double> target_positions(request->data.data.begin(), request->data.data.end());
    double trajectory_time = request->time;

    RCLCPP_INFO(this->get_logger(), "Target (6 DOF): [%.3f, %.3f, %.3f, %.3f, %.3f, %.3f], Time: %.2fs",
                target_positions.size() > 0 ? target_positions[0] : 0.0,
                target_positions.size() > 1 ? target_positions[1] : 0.0,
                target_positions.size() > 2 ? target_positions[2] : 0.0,
                target_positions.size() > 3 ? target_positions[3] : 0.0,
                target_positions.size() > 4 ? target_positions[4] : 0.0,
                target_positions.size() > 5 ? target_positions[5] : 0.0,
                trajectory_time);

    // goto_js_v2 uses scheduled gains (near/far interpolated by extension)
    response->success = planAndExecuteTrajectory(target_positions, trajectory_time, GainMode::SCHEDULED);

    if (response->success) {
        RCLCPP_INFO(this->get_logger(), "Successfully planned trajectory (v2)");
    } else {
        RCLCPP_ERROR(this->get_logger(), "Failed to plan trajectory (v2)");
    }
}

} // namespace maurice_arm
