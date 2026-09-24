// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/int32.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_msgs/msg/empty.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <std_srvs/srv/set_bool.hpp>
#include "mars_arm/dynamixel.hpp"
#include "mars_arm/robot.hpp"
#include "mars_arm/arm_types.hpp"
#include "mars_msgs/srv/goto_js.hpp"
#include "mars_msgs/srv/goto_js_trajectory.hpp"
#include "mars_msgs/msg/arm_status.hpp"
#include <atomic>
#include <cmath>
#include <chrono>
#include <nlohmann/json.hpp>
#include <thread>
#include <sstream>
#include <iomanip>
#include <cstdint>
#include <set>
#include <array>
#include <deque>
#include <mutex>
#include <atomic>

namespace mars_arm {

class MarsArmNode : public rclcpp::Node {
   public:
    MarsArmNode();
    ~MarsArmNode() = default;

    // ── Configuration (arm_config.cpp) ──────────────────────────────────
    void loadJointConfigs(const std::vector<std::string>& joint_names);
    rcl_interfaces::msg::SetParametersResult onParameterChange(const std::vector<rclcpp::Parameter>& parameters);

    // ── Servo init & helpers (arm_services.cpp) ─────────────────────────
    void initializeServos();
    template <typename Func>
    void retryServoOp(int servo_id, const char* op_name, Func&& fn, int max_retries = 3);
    void configureServoByIdLocked(int servo_id, bool enable_torque = true);
    // Re-write Goal Current after any torque enable (it is RAM and a
    // torque-enable clears it). No-op for joints without a goal_current.
    void reapplyGoalCurrentLocked(int servo_id);
    void configureServosLocked(bool enable_torque = true);
    void syncTargetToMotorPositions();

    // ── Control loop (arm_control.cpp) ──────────────────────────────────
    void controlTimerCallback();
    void recordLoopTiming(std::array<std::chrono::steady_clock::time_point, 9>& ts);
    std::vector<int> applyLimitsAndConvertToEncoder(std::vector<double>& command_data);

    // ── Service & topic callbacks (arm_services.cpp) ────────────────────
    void armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg);
    void armTorqueOnCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
                             std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armTorqueOffCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
                              std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armRebootServosCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
                                 std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armFixErrorCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
                             std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void healthMonitorCallback();
    std::string describeHardwareError(uint8_t status, int servo_id) const;
    bool autoRecoverServoLocked(int servo_id);

    // Head control
    int logicalAngleToEncoder(double logical_angle_deg);
    double encoderToLogicalAngle(int encoder_value);
    void moveHeadToAngle(double logical_angle_deg);
    void moveHeadToAngleLocked(double logical_angle_deg);
    void publishHeadPosition(int encoder_value);
    void headPositionCallback(const std_msgs::msg::Int32::SharedPtr msg);
    void headAiPositionCallback(const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
                                std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void headEnableServoCallback(const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
                                 std::shared_ptr<std_srvs::srv::SetBool::Response> response);

    // ── Trajectory planning & execution (arm_trajectory.cpp) ────────────
    std::vector<std::vector<double>> computeCubicSplineTrajectory(const std::vector<double>& start,
                                                                  const std::vector<double>& goal, double duration,
                                                                  double dt);
    bool planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time,
                                  GainMode trajectory_gain_mode = GainMode::SCHEDULED);
    bool planAndExecuteMultiWaypointTrajectory(const std::vector<std::vector<double>>& waypoints,
                                               const std::vector<double>& segment_durations);
    void armGotoJSCallback(const std::shared_ptr<mars_msgs::srv::GotoJS::Request> request,
                           std::shared_ptr<mars_msgs::srv::GotoJS::Response> response);
    void armGotoJSV2Callback(const std::shared_ptr<mars_msgs::srv::GotoJS::Request> request,
                             std::shared_ptr<mars_msgs::srv::GotoJS::Response> response);
    void armGotoJSTrajectoryCallback(const std::shared_ptr<mars_msgs::srv::GotoJSTrajectory::Request> request,
                                     std::shared_ptr<mars_msgs::srv::GotoJSTrajectory::Response> response);

    // ── Member variables ────────────────────────────────────────────────

    std::shared_ptr<Dynamixel> dynamixel_;
    std::unique_ptr<Robot> robot_;
    std::vector<JointConfig> joint_configs_;

    // ARM publishers / subscribers / services
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr arm_state_pub_;
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr arm_command_state_pub_;
    rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr arm_command_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_on_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_off_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_reboot_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_fix_error_service_;
    rclcpp::Service<mars_msgs::srv::GotoJS>::SharedPtr arm_goto_js_service_;
    rclcpp::Service<mars_msgs::srv::GotoJS>::SharedPtr arm_goto_js_v2_service_;
    rclcpp::Service<mars_msgs::srv::GotoJSTrajectory>::SharedPtr arm_goto_js_traj_service_;
    sensor_msgs::msg::JointState arm_state_msg_;    // 6-joint message for /mars/arm/state
    sensor_msgs::msg::JointState joint_state_msg_;  // 7-joint message for /joint_states
    std::vector<int> latest_arm_command_;
    std::mutex arm_command_mutex_;
    std::atomic<bool> has_arm_command_{false};

    // Direct pass-through (guarded by arm_command_mutex_)
    std::array<double, 6> latest_target_{};
    bool has_target_{false};

    // Joint state tracking for planning
    std::vector<double> latest_joint_positions_;
    std::mutex joint_state_mutex_;

    std::vector<std::string> joint_names_{"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};

    // HEAD members
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr head_position_pub_;
    rclcpp::Subscription<std_msgs::msg::Int32>::SharedPtr head_position_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr head_ai_position_service_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr head_enable_service_;
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_state_pub_;
    int latest_head_command_{0};
    std::mutex head_command_mutex_;
    std::atomic<bool> has_head_command_{false};

    // Health monitoring
    rclcpp::Publisher<mars_msgs::msg::ArmStatus>::SharedPtr arm_status_pub_;
    rclcpp::TimerBase::SharedPtr health_timer_;
    mars_msgs::msg::ArmStatus last_arm_status_;
    // Auto-recovery reboot timestamps per servo id (1-7); only touched from
    // the health timer's callback group, so no lock of its own.
    std::array<std::deque<std::chrono::steady_clock::time_point>, 8> auto_recovery_history_;
    // Servos an auto-recovery rebooted but did not finish reconfiguring. The
    // reboot clears the latched error bit, so nothing else would flag them —
    // the health check retries the configuration and holds the arm unhealthy
    // until it sticks. Same callback group as auto_recovery_history_.
    std::array<bool, 8> auto_recovery_incomplete_{};
    std::atomic<bool> arm_torque_enabled_{true};

    // Control timer
    rclcpp::TimerBase::SharedPtr control_timer_;
    double control_frequency_;

    // Callback groups for parallel execution
    rclcpp::CallbackGroup::SharedPtr timer_callback_group_;
    rclcpp::CallbackGroup::SharedPtr service_callback_group_;
    rclcpp::CallbackGroup::SharedPtr health_callback_group_;

    // Mutex to protect Dynamixel serial bus access
    std::mutex dynamixel_mutex_;

    // PID hot-reload
    rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr param_callback_handle_;

    // Gain scheduling
    std::array<GainProfile, 7> gs_near_;
    std::array<GainProfile, 7> gs_far_;
    std::array<GainProfile, 7> gs_teleop_;
    std::array<GainProfile, 7> gs_last_applied_;
    int gs_cycle_counter_ = 0;
    // Written by the trajectory and service threads, read (and decayed) by
    // the control loop — atomic so those cross-thread accesses are defined.
    // The decay's check-then-write can still interleave with a trajectory
    // start; the waypoint loop re-asserts the mode each step to bound that.
    std::atomic<GainMode> gain_mode_{GainMode::TELEOP};
    GainMode last_applied_gain_mode_{GainMode::TELEOP};
    // When the last trajectory finished. Scheduled gains hold the arm firmly
    // between the closely-spaced moves a skill sends (dropping to the soft
    // teleop gains in the gaps made the arm sag and snap back), but holding
    // them while idle cooks the shoulder — joint 2 reached 70 C. So the hold
    // decays back to teleop gains after kScheduledHoldTimeout of quiet.
    // Atomic for the same reason as gain_mode_: stamped by the trajectory
    // threads (HoldGuard), read by the control loop.
    std::atomic<std::chrono::steady_clock::time_point> last_trajectory_end_{std::chrono::steady_clock::time_point{}};
    // True while a trajectory is streaming waypoints. The idle decay must
    // never fire mid-trajectory: last_trajectory_end_ is stale during
    // execution, and the decay once flipped a 3 s carry move onto soft
    // teleop gains 3 ms after it started — the shaken grip dropped the
    // object it was carrying.
    std::atomic<bool> trajectory_executing_{false};

    // Control loop timing instrumentation
    std::array<TimingAccumulator, 10> timing_stats_{{TimingAccumulator{"total"}, TimingAccumulator{"lock_wait"},
                                                     TimingAccumulator{"readState"}, TimingAccumulator{"readEffort"},
                                                     TimingAccumulator{"pubArm"}, TimingAccumulator{"pubHead+JS"},
                                                     TimingAccumulator{"gainSched"}, TimingAccumulator{"cmdWrite"},
                                                     TimingAccumulator{"syncWritePID"}, TimingAccumulator{"misc"}}};
};

// Template implementation must be in the header
template <typename Func>
void MarsArmNode::retryServoOp(int servo_id, const char* op_name, Func&& fn, int max_retries) {
    for (int attempt = 1; attempt <= max_retries; ++attempt) {
        try {
            fn();
            return;
        } catch (const std::exception& e) {
            RCLCPP_WARN(this->get_logger(), "Servo %d %s attempt %d/%d failed: %s", servo_id, op_name, attempt,
                        max_retries, e.what());
            if (attempt == max_retries)
                throw;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
    }
}

}  // namespace mars_arm
