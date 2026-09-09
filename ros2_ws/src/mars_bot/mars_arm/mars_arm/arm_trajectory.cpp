// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// arm_trajectory.cpp — Trajectory planning and execution
#include "mars_arm/arm_node.hpp"

namespace mars_arm {

// ========== CUBIC SPLINE (QUINTIC SMOOTHERSTEP) ==========

std::vector<std::vector<double>> MarsArmNode::computeCubicSplineTrajectory(const std::vector<double>& start,
                                                                           const std::vector<double>& goal,
                                                                           double duration, double dt) {
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
                        "Jerk limit %.1f rad/s³: extending trajectory %.2fs → %.2fs (Δθ_max=%.3f rad)", max_jerk,
                        duration, min_duration, max_delta);
            duration = min_duration;
        }
    }

    int num_steps = static_cast<int>(duration / dt);
    if (num_steps < 1)
        num_steps = 1;

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

bool MarsArmNode::planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time,
                                           GainMode trajectory_gain_mode, TrajectoryGuard* guard) {
    // Block the idle gain decay for the whole call; the guard stamps the
    // quiet period's start on every exit path.
    trajectory_executing_ = true;
    struct HoldGuard {
        MarsArmNode* n;
        ~HoldGuard() {
            n->last_trajectory_end_ = std::chrono::steady_clock::now();
            n->trajectory_executing_ = false;
        }
    } hold_guard{this};

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

    // Gripper (j6) is current-based position control: the standing position
    // error IS the grip force, so spline from the last COMMANDED goal — the
    // measured stall position would zero the preload and drop the object.
    {
        std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
        if (has_target_) {
            current_positions[5] = latest_target_[5];
        }
    }

    // Use simple cubic spline planning (fast, smooth trajectory)
    RCLCPP_INFO(this->get_logger(), "Planning with cubic spline for 6-DOF arm (including gripper)...");
    const double dt = 1.0 / this->get_parameter("trajectory_rate_hz").as_double();
    auto interpolated_trajectory =
        computeCubicSplineTrajectory(current_positions, target_positions, trajectory_time, dt);

    if (interpolated_trajectory.empty()) {
        RCLCPP_ERROR(this->get_logger(), "Cubic spline trajectory computation failed");
        return false;
    }

    // Detect if jerk limiting extended the duration
    double actual_duration = (interpolated_trajectory.size() - 1) * dt;
    if (actual_duration > trajectory_time * 1.01) {
        RCLCPP_WARN(this->get_logger(), "Jerk-limited: requested %.2fs but executing %.2fs (+%.0f%%)", trajectory_time,
                    actual_duration, 100.0 * (actual_duration - trajectory_time) / trajectory_time);
    }

    RCLCPP_INFO(this->get_logger(), "Executing trajectory with %zu waypoints over %.2f seconds",
                interpolated_trajectory.size(), actual_duration);

    // Execute trajectory by sending each waypoint with a sleep
    auto sleep_duration = std::chrono::duration<double>(dt);
    for (size_t i = 0; i < interpolated_trajectory.size(); ++i) {
        const auto& point = interpolated_trajectory[i];

        if (guard != nullptr && guardTripped(*guard)) {
            if (guard->blocked_joint >= 0) {
                holdArmWhereItIs();
            }
            RCLCPP_WARN(this->get_logger(), "Trajectory stopped: %s", guard->stop_reason.c_str());
            return false;
        }

        // Re-assert per waypoint: an idle-decay check racing the switch above
        // can stomp the mode once, leaving the trajectory on soft gains.
        gain_mode_ = trajectory_gain_mode;

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

    // Deliberately KEEP the trajectory's gain mode for the hold: dropping to
    // teleop gains here sags the arm between a skill's stepped moves. The
    // control loop decays it after a quiet period — holding an idle arm stiff
    // overheated joint 2.
    return true;
}

bool MarsArmNode::guardTripped(TrajectoryGuard& guard) {
    if (!arm_torque_enabled_) {
        guard.stop_reason = "arm torque went off";
        return true;
    }
    if (stream_command_at_.load() > guard.started) {
        guard.stop_reason = "a streaming arm command took over";
        return true;
    }
    std::array<double, 6> written{};
    {
        std::lock_guard<std::mutex> lock(arm_command_mutex_);
        if (written_at_ < guard.started) {
            return false;  // the control loop has not written this trajectory yet
        }
        written = written_target_;
    }
    std::vector<double> measured;
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        measured = latest_joint_positions_;
    }
    const double waited_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - guard.started).count();
    int blocked_joint = -1;
    double blocked_error = 0.0;
    for (size_t j = 0; j < kArmJoints && j < measured.size(); ++j) {
        const double error = std::abs(measured[j] - written[j]);
        if (error <= guard.max_error_rad) {
            guard.locked_on[j] = true;
            guard.strikes[j] = 0;
            continue;
        }
        if (!guard.locked_on[j] && waited_s < kContactLockOnTimeoutS) {
            continue;
        }
        if (++guard.strikes[j] >= kContactStrikes && error > blocked_error) {
            blocked_error = error;
            blocked_joint = static_cast<int>(j);
        }
    }
    if (blocked_joint < 0) {
        return false;
    }
    guard.blocked_joint = blocked_joint;
    std::ostringstream reason;
    reason << "joint " << blocked_joint + 1 << " met resistance (" << std::fixed << std::setprecision(2)
           << blocked_error << " rad behind its command); holding the arm where it is";
    guard.stop_reason = reason.str();
    return true;
}

// Retarget the arm to where it actually is so the servos stop pushing.
void MarsArmNode::holdArmWhereItIs() {
    std::vector<double> measured;
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        measured = latest_joint_positions_;
    }
    std::lock_guard<std::mutex> lock(arm_command_mutex_);
    for (size_t j = 0; j < kArmJoints && j < measured.size(); ++j) {
        latest_target_[j] = measured[j];
    }
    has_target_ = true;
}

// ========== REST FOLD ==========

void MarsArmNode::idleRestCallback() {
    if (!this->get_parameter("auto_rest").as_bool() || !arm_torque_enabled_ || trajectory_executing_) {
        return;
    }
    const auto last_command =
        std::max({stream_command_at_.load(), last_trajectory_end_.load(), last_service_at_.load()});
    if (std::chrono::duration<double>(std::chrono::steady_clock::now() - last_command).count() < kRestWhenIdleS) {
        return;
    }
    std::vector<double> measured;
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        measured = latest_joint_positions_;
    }
    if (measured.size() != 6 || !onFloor(measured[1], measured[2], measured[3])) {
        return;
    }
    if (idle_rest_acted_) {
        double moved = 0.0;
        for (size_t j = 0; j < kArmJoints; ++j) {
            moved = std::max(moved, std::abs(measured[j] - idle_rest_acted_at_[j]));
        }
        if (moved < kAtRestRad) {
            return;
        }
    }
    foldToRest("idle");
    idle_rest_acted_ = true;
    std::lock_guard<std::mutex> lock(joint_state_mutex_);
    std::copy_n(latest_joint_positions_.begin(), std::min<size_t>(6, latest_joint_positions_.size()),
                idle_rest_acted_at_.begin());
}

RestOutcome MarsArmNode::foldToRest(const char* trigger) {
    const RestOutcome outcome = runRestFold(trigger);
    if (outcome.at_rest) {
        RCLCPP_INFO(this->get_logger(), "Rest fold (%s): %s", trigger, outcome.detail.c_str());
    } else {
        RCLCPP_WARN(this->get_logger(), "Rest fold (%s): %s", trigger, outcome.detail.c_str());
    }
    return outcome;
}

RestOutcome MarsArmNode::runRestFold(const char* trigger) {
    if (!arm_torque_enabled_) {
        return {false, "rest fold skipped: arm torque is off"};
    }
    std::vector<double> rest = this->get_parameter("rest_pose").as_double_array();
    if (rest.size() != 6) {
        return {false, "rest fold skipped: rest_pose must list 6 joint positions"};
    }
    std::vector<double> measured;
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        measured = latest_joint_positions_;
    }
    if (measured.size() != 6) {
        return {false, "rest fold skipped: no joint state yet"};
    }
    double grip;
    {
        std::lock_guard<std::mutex> lock(arm_command_mutex_);
        // j6 is current-based position control: re-commanding it above the
        // standing grip target zeroes the preload and drops a held object.
        grip = has_target_ ? latest_target_[5] : measured[5];
    }
    double away = 0.0;
    for (size_t j = 0; j < kArmJoints; ++j) {
        away = std::max(away, std::abs(measured[j] - rest[j]));
    }
    if (away < kAtRestRad) {
        return {true, "arm already at rest"};
    }
    RCLCPP_INFO(this->get_logger(), "Folding the arm to rest (%s)", trigger);
    std::string stopped;
    if (onFloor(measured[1], measured[2], measured[3]) && !liftOffTheFloor(measured, grip, stopped)) {
        return {false, "rest lift stopped: " + stopped};
    }
    rest[5] = grip;
    const bool shoulder_blocked = shoulderMinLimit(measured[0]) > rest[1];
    if (shoulder_blocked) {
        std::vector<double> clear = rest;
        clear[1] = kShoulderClearanceRad;
        if (!foldStage(clear, kRestFoldDurationS, stopped)) {
            return {false, "rest fold stopped: " + stopped};
        }
    }
    if (foldStage(rest, shoulder_blocked ? kRestShoulderDurationS : kRestFoldDurationS, stopped)) {
        return {true, "arm folded to rest"};
    }
    return {false, "rest fold stopped: " + stopped};
}

// See kOnFloorM: the tip goes up before anything slides along the floor.
bool MarsArmNode::liftOffTheFloor(const std::vector<double>& measured, double grip, std::string& stopped) {
    std::vector<double> lift = measured;
    lift[5] = grip;
    const double shoulder = std::max(kLiftShoulderRad, shoulderMinLimit(measured[0]));
    const double pitch = measured[1] + measured[2] + measured[3];  // gripper angle below horizontal
    if (pitch < kFlatGripperRad && measured[1] > shoulder + kAtRestRad) {
        lift[1] = shoulder;
        RCLCPP_INFO(this->get_logger(), "Pivoting the flat gripper up off the ground first");
        if (!foldStage(lift, kRestShoulderDurationS, stopped)) {
            return false;
        }
    } else if (pitch >= kFlatGripperRad && pitch < kWristLevelMaxPitchRad) {
        lift[3] = clampToJointRange(3, measured[3] - pitch);
        RCLCPP_INFO(this->get_logger(), "Levelling the gripper to lift its tip off the ground first");
        if (!foldStage(lift, kRestLevelDurationS, stopped)) {
            return false;
        }
    }
    lift[1] = shoulder;
    lift[2] = kLiftElbowRad;
    RCLCPP_INFO(this->get_logger(), "Lifting the gripper clear of the ground");
    return foldStage(lift, kRestLiftDurationS, stopped);
}

bool MarsArmNode::foldStage(const std::vector<double>& target, double duration, std::string& stopped) {
    TrajectoryGuard guard{kRestContactErrorRad};
    if (planAndExecuteTrajectory(target, duration, GainMode::SCHEDULED, &guard)) {
        return true;
    }
    stopped = guard.stop_reason;
    return false;
}

void MarsArmNode::armRestCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
                                  std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/rest");
    last_service_at_ = std::chrono::steady_clock::now();
    const RestOutcome outcome = foldToRest("service");
    response->success = outcome.at_rest;
    response->message = outcome.detail;
}

bool MarsArmNode::planAndExecuteMultiWaypointTrajectory(const std::vector<std::vector<double>>& waypoints,
                                                        const std::vector<double>& segment_durations) {
    // See planAndExecuteTrajectory: block the idle gain decay while executing.
    trajectory_executing_ = true;
    struct HoldGuard {
        MarsArmNode* n;
        ~HoldGuard() {
            n->last_trajectory_end_ = std::chrono::steady_clock::now();
            n->trajectory_executing_ = false;
        }
    } hold_guard{this};

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

        // Re-assert per waypoint — see planAndExecuteTrajectory
        gain_mode_ = GainMode::SCHEDULED;

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

    // Deliberately KEEP scheduled gains for the hold (see the single-target
    // version above) — the control loop decays them after a quiet period.
    return true;
}

// ========== SERVICE CALLBACKS ==========

void MarsArmNode::armGotoJSTrajectoryCallback(const std::shared_ptr<mars_msgs::srv::GotoJSTrajectory::Request> request,
                                              std::shared_ptr<mars_msgs::srv::GotoJSTrajectory::Response> response) {
    RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/goto_js_trajectory");

    int num_joints = request->num_joints;
    const auto& flat = request->waypoints.data;
    const auto& seg_durs = request->segment_durations;

    if (num_joints <= 0 || flat.size() % num_joints != 0) {
        RCLCPP_ERROR(this->get_logger(), "Invalid waypoints: %zu values not divisible by %d joints", flat.size(),
                     num_joints);
        response->success = false;
        return;
    }

    // Unpack flat array into waypoint vectors
    size_t num_waypoints = flat.size() / num_joints;
    std::vector<std::vector<double>> waypoints;
    for (size_t i = 0; i < num_waypoints; ++i) {
        waypoints.emplace_back(flat.begin() + i * num_joints, flat.begin() + (i + 1) * num_joints);
    }

    std::vector<double> durations(seg_durs.begin(), seg_durs.end());

    RCLCPP_INFO(this->get_logger(), "Trajectory: %zu waypoints, %zu segments", waypoints.size(), durations.size());

    // Prepend current position as waypoint[0] so the arm starts from where it is
    {
        std::lock_guard<std::mutex> lock(joint_state_mutex_);
        if (!latest_joint_positions_.empty()) {
            std::vector<double> start = latest_joint_positions_;
            // Gripper starts from the last COMMANDED goal (see
            // planAndExecuteTrajectory): seeding it at the measured stall
            // position would zero the grip preload.
            {
                std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
                if (has_target_ && start.size() >= 6) {
                    start[5] = latest_target_[5];
                }
            }
            waypoints.insert(waypoints.begin(), start);
            // One duration per waypoint means durations[0] already paces this
            // prepended approach; legacy waypoints-1 callers get a copy of the
            // first segment instead.
            if (durations.size() + 1 < waypoints.size()) {
                durations.insert(durations.begin(), durations.empty() ? 0.5 : durations[0]);
            }
        } else if (!durations.empty() && durations.size() == waypoints.size()) {
            // No current pose to prepend: the approach duration has no segment.
            durations.erase(durations.begin());
        }
    }

    response->success = planAndExecuteMultiWaypointTrajectory(waypoints, durations);
}

void MarsArmNode::armGotoJSCallback(const std::shared_ptr<mars_msgs::srv::GotoJS::Request> request,
                                    std::shared_ptr<mars_msgs::srv::GotoJS::Response> response) {
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
                target_positions.size() > 5 ? target_positions[5] : 0.0, trajectory_time);

    // goto_js uses teleop gains (flat, no extension-based interpolation)
    response->success = planAndExecuteTrajectory(target_positions, trajectory_time, GainMode::TELEOP);

    if (response->success) {
        RCLCPP_INFO(this->get_logger(), "Successfully planned trajectory");
    } else {
        RCLCPP_ERROR(this->get_logger(), "Failed to plan trajectory");
    }
}

void MarsArmNode::armGotoJSV2Callback(const std::shared_ptr<mars_msgs::srv::GotoJS::Request> request,
                                      std::shared_ptr<mars_msgs::srv::GotoJS::Response> response) {
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
                target_positions.size() > 5 ? target_positions[5] : 0.0, trajectory_time);

    // goto_js_v2 uses scheduled gains (near/far interpolated by extension)
    response->success = planAndExecuteTrajectory(target_positions, trajectory_time, GainMode::SCHEDULED);

    if (response->success) {
        RCLCPP_INFO(this->get_logger(), "Successfully planned trajectory (v2)");
    } else {
        RCLCPP_ERROR(this->get_logger(), "Failed to plan trajectory (v2)");
    }
}

}  // namespace mars_arm
