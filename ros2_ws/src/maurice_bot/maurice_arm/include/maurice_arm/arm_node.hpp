#pragma once

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/int32.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_msgs/msg/empty.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <std_srvs/srv/set_bool.hpp>
#include "maurice_arm/dynamixel.hpp"
#include "maurice_arm/robot.hpp"
#include "maurice_arm/arm_types.hpp"
#include "maurice_msgs/srv/goto_js.hpp"
#include "maurice_msgs/srv/goto_js_trajectory.hpp"
#include "maurice_msgs/msg/arm_status.hpp"
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

namespace maurice_arm {

class MauriceArmNode : public rclcpp::Node {
public:
    MauriceArmNode();
    ~MauriceArmNode() = default;

    // ── Configuration (arm_config.cpp) ──────────────────────────────────
    void loadJointConfigs(const std::vector<std::string>& joint_names);
    rcl_interfaces::msg::SetParametersResult onParameterChange(
        const std::vector<rclcpp::Parameter>& parameters);

    // ── Servo init & helpers (arm_services.cpp) ─────────────────────────
    void initializeServos();
    template<typename Func>
    void retryServoOp(int servo_id, const char* op_name, Func&& fn, int max_retries = 3);
    void configureServoByIdLocked(int servo_id, bool enable_torque = true);
    void configureServosLocked(bool enable_torque = true);

    // ── Control loop (arm_control.cpp) ──────────────────────────────────
    void controlTimerCallback();
    void recordLoopTiming(std::array<std::chrono::steady_clock::time_point, 9>& ts);
    std::vector<int> applyLimitsAndConvertToEncoder(std::vector<double>& command_data);

    // ── Motor stress protection (arm_control.cpp) ───────────────────────
    void updateMotorStress(double dt, const std::vector<int>& loads);

    // ── Service & topic callbacks (arm_services.cpp) ────────────────────
    void armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg);
    void armTorqueOnCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armTorqueOffCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armRebootServosCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void armFixErrorCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void healthMonitorCallback();
    std::string describeHardwareError(uint8_t status, int servo_id) const;

    // Head control
    int logicalAngleToEncoder(double logical_angle_deg);
    double encoderToLogicalAngle(int encoder_value);
    void moveHeadToAngle(double logical_angle_deg);
    void moveHeadToAngleLocked(double logical_angle_deg);
    void publishHeadPosition(int encoder_value);
    void headPositionCallback(const std_msgs::msg::Int32::SharedPtr msg);
    void headAiPositionCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void headEnableServoCallback(
        const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
        std::shared_ptr<std_srvs::srv::SetBool::Response> response);

    // ── Trajectory planning & execution (arm_trajectory.cpp) ────────────
    std::vector<std::vector<double>> computeCubicSplineTrajectory(
        const std::vector<double>& start,
        const std::vector<double>& goal,
        double duration,
        double dt);
    bool planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time,
                                   GainMode trajectory_gain_mode = GainMode::SCHEDULED);
    bool planAndExecuteMultiWaypointTrajectory(
        const std::vector<std::vector<double>>& waypoints,
        const std::vector<double>& segment_durations);
    void armGotoJSCallback(
        const std::shared_ptr<maurice_msgs::srv::GotoJS::Request> request,
        std::shared_ptr<maurice_msgs::srv::GotoJS::Response> response);
    void armGotoJSV2Callback(
        const std::shared_ptr<maurice_msgs::srv::GotoJS::Request> request,
        std::shared_ptr<maurice_msgs::srv::GotoJS::Response> response);
    void armGotoJSTrajectoryCallback(
        const std::shared_ptr<maurice_msgs::srv::GotoJSTrajectory::Request> request,
        std::shared_ptr<maurice_msgs::srv::GotoJSTrajectory::Response> response);

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
    rclcpp::Service<maurice_msgs::srv::GotoJS>::SharedPtr arm_goto_js_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJS>::SharedPtr arm_goto_js_v2_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJSTrajectory>::SharedPtr arm_goto_js_traj_service_;
    sensor_msgs::msg::JointState arm_state_msg_;   // 6-joint message for /mars/arm/state
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

    // Joint names and home position
    std::vector<std::string> joint_names_{"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
    std::vector<double> home_position_;

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
    rclcpp::Publisher<maurice_msgs::msg::ArmStatus>::SharedPtr arm_status_pub_;
    rclcpp::TimerBase::SharedPtr health_timer_;
    maurice_msgs::msg::ArmStatus last_arm_status_;
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
    GainMode gain_mode_{GainMode::SCHEDULED};
    GainMode last_applied_gain_mode_{GainMode::SCHEDULED};

    // Motor stress protection (overload cooldown)
    // Leaky integrator: score += (A * |PWM| - C) * dt, clamped >= 0
    std::array<MotorStressTracker, 7> stress_trackers_;
    bool stress_enabled_{false};              // master enable for leaky integrator
    double stress_threshold_{100.0};      // score at which cooldown triggers
    double stress_cooldown_sec_{2.0};     // how long to rest (seconds)
    double stress_multiplier_{1.0};       // A: multiplier on |load|
    double stress_leak_{0.0};             // C: constant leak rate

    // Control loop timing instrumentation
    std::array<TimingAccumulator, 10> timing_stats_{{
        TimingAccumulator{"total"},
        TimingAccumulator{"lock_wait"},
        TimingAccumulator{"readState"},
        TimingAccumulator{"readEffort"},
        TimingAccumulator{"pubArm"},
        TimingAccumulator{"pubHead+JS"},
        TimingAccumulator{"gainSched"},
        TimingAccumulator{"cmdWrite"},
        TimingAccumulator{"syncWritePID"},
        TimingAccumulator{"misc"}
    }};
};

// Template implementation must be in the header
template<typename Func>
void MauriceArmNode::retryServoOp(int servo_id, const char* op_name, Func&& fn, int max_retries) {
    for (int attempt = 1; attempt <= max_retries; ++attempt) {
        try {
            fn();
            return;
        } catch (const std::exception& e) {
            RCLCPP_WARN(this->get_logger(), "Servo %d %s attempt %d/%d failed: %s",
                servo_id, op_name, attempt, max_retries, e.what());
            if (attempt == max_retries) throw;
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
    }
}

} // namespace maurice_arm
