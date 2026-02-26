// arm_trajectory.cpp — Trajectory planning and execution
#include "maurice_arm/arm_node.hpp"

namespace maurice_arm {

// ========== MOVEIT PLANNING ==========

std::pair<std::vector<std::vector<double>>, std::vector<double>>
MauriceArmNode::planWithMoveIt(const std::vector<double>& start, const std::vector<double>& goal, double planning_time) {

    if (!moveit_available_) {
        RCLCPP_ERROR(this->get_logger(), "MoveIt is not available");
        return {};
    }

    // Create motion plan request
    auto request = std::make_shared<moveit_msgs::srv::GetMotionPlan::Request>();

    // Set planning group and parameters
    request->motion_plan_request.group_name = "arm";
    request->motion_plan_request.num_planning_attempts = 3;
    request->motion_plan_request.allowed_planning_time = planning_time;
    request->motion_plan_request.planner_id = "RRTConnect";

    // Set start state
    request->motion_plan_request.start_state.joint_state.name = joint_names_;
    request->motion_plan_request.start_state.joint_state.position = start;
    request->motion_plan_request.start_state.is_diff = false;

    // Set goal constraints (joint space goal)
    moveit_msgs::msg::Constraints goal_constraint;
    for (size_t i = 0; i < goal.size(); ++i) {
        moveit_msgs::msg::JointConstraint jc;
        jc.joint_name = joint_names_[i];
        jc.position = goal[i];
        jc.tolerance_above = 0.01;
        jc.tolerance_below = 0.01;
        jc.weight = 1.0;
        goal_constraint.joint_constraints.push_back(jc);
    }
    request->motion_plan_request.goal_constraints.push_back(goal_constraint);

    // Set workspace parameters
    request->motion_plan_request.workspace_parameters.header.frame_id = "base_link";
    request->motion_plan_request.workspace_parameters.min_corner.x = -1.0;
    request->motion_plan_request.workspace_parameters.min_corner.y = -1.0;
    request->motion_plan_request.workspace_parameters.min_corner.z = -0.5;
    request->motion_plan_request.workspace_parameters.max_corner.x = 1.0;
    request->motion_plan_request.workspace_parameters.max_corner.y = 1.0;
    request->motion_plan_request.workspace_parameters.max_corner.z = 1.0;

    // Call service
    RCLCPP_INFO(this->get_logger(), "Calling MoveIt planning service...");
    auto future = moveit_plan_client_->async_send_request(request);

    // Wait for response
    auto timeout = std::chrono::duration<double>(planning_time + 5.0);
    if (future.wait_for(timeout) != std::future_status::ready) {
        RCLCPP_ERROR(this->get_logger(), "MoveIt planning service call timed out");
        return {};
    }

    auto response = future.get();

    // Check if planning succeeded (error_code.val == 1 means SUCCESS)
    if (response->motion_plan_response.error_code.val != 1) {
        RCLCPP_ERROR(this->get_logger(), "MoveIt planning failed with error code %d",
                    response->motion_plan_response.error_code.val);
        return {};
    }

    // Extract trajectory waypoints and timing
    auto& trajectory = response->motion_plan_response.trajectory.joint_trajectory;
    std::vector<std::vector<double>> waypoints;
    std::vector<double> time_from_start;

    for (const auto& point : trajectory.points) {
        waypoints.push_back(std::vector<double>(point.positions.begin(), point.positions.end()));
        double time_sec = point.time_from_start.sec + point.time_from_start.nanosec * 1e-9;
        time_from_start.push_back(time_sec);
    }

    RCLCPP_INFO(this->get_logger(), "MoveIt planning succeeded with %zu waypoints", waypoints.size());
    return {waypoints, time_from_start};
}

// ========== TRAJECTORY INTERPOLATION ==========

std::vector<std::vector<double>> MauriceArmNode::interpolateMoveItTrajectory(
    const std::vector<std::vector<double>>& waypoints,
    const std::vector<double>& time_from_start,
    double dt) {

    if (waypoints.empty() || time_from_start.empty()) {
        return {};
    }

    std::vector<std::vector<double>> trajectory;
    double total_duration = time_from_start.back();
    double current_time = 0.0;

    RCLCPP_INFO(this->get_logger(), "Interpolating trajectory: %zu waypoints, %.2f sec duration, %.3f sec timestep",
                waypoints.size(), total_duration, dt);

    while (current_time <= total_duration) {
        // Find the two waypoints to interpolate between
        size_t wp_idx = 0;
        for (size_t i = 0; i < time_from_start.size() - 1; ++i) {
            if (current_time <= time_from_start[i + 1]) {
                wp_idx = i;
                break;
            }
            wp_idx = i + 1;
        }

        if (wp_idx >= waypoints.size() - 1) {
            trajectory.push_back(waypoints.back());
            break;
        }

        // Linear interpolation between waypoints
        double t1 = time_from_start[wp_idx];
        double t2 = time_from_start[wp_idx + 1];

        if (t2 > t1) {
            double alpha = (current_time - t1) / (t2 - t1);
            const auto& wp1 = waypoints[wp_idx];
            const auto& wp2 = waypoints[wp_idx + 1];

            std::vector<double> interpolated(wp1.size());
            for (size_t j = 0; j < wp1.size(); ++j) {
                interpolated[j] = wp1[j] + alpha * (wp2[j] - wp1[j]);
            }
            trajectory.push_back(interpolated);
        } else {
            trajectory.push_back(waypoints[wp_idx]);
        }

        current_time += dt;
    }

    RCLCPP_INFO(this->get_logger(), "Interpolation complete: %zu trajectory points", trajectory.size());
    return trajectory;
}

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

bool MauriceArmNode::planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time) {
    // Switch to scheduled gains for planned trajectories
    if (gain_mode_ != GainMode::SCHEDULED) {
        gain_mode_ = GainMode::SCHEDULED;
        RCLCPP_INFO(this->get_logger(), "Gain mode -> SCHEDULED (trajectory execution)");
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

        // Send command
        {
            std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
            std::vector<double> command_data = point;
            latest_arm_command_ = applyLimitsAndConvertToEncoder(command_data);
            has_arm_command_ = true;
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
            std::vector<double> command_data = point;
            latest_arm_command_ = applyLimitsAndConvertToEncoder(command_data);
            has_arm_command_ = true;
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

    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/goto_js");

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

    // Call internal planning function (6 joints including gripper)
    response->success = planAndExecuteTrajectory(target_positions, trajectory_time);

    if (response->success) {
        RCLCPP_INFO(this->get_logger(), "Successfully planned trajectory");
    } else {
        RCLCPP_ERROR(this->get_logger(), "Failed to plan trajectory");
    }
}

} // namespace maurice_arm
