// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// arm_node.cpp — Constructor, main()
#include "mars_arm/arm_node.hpp"

#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace mars_arm {

namespace {

// Scans /sys/class/tty/ttyACM* and returns the /dev path whose USB parent
// device matches the CH343 VID:PID (1a86:55d3) used by the arm MCU.
// Falls back to /dev/ttyACM0 on no match.
std::string discoverArmDevice(const rclcpp::Logger& logger) {
    namespace fs = std::filesystem;
    static constexpr const char* kTargetVid = "1a86";  // WCH
    static constexpr const char* kTargetPid = "55d3";  // CH343 "USB Single Serial"
    static constexpr const char* kTtyClass = "/sys/class/tty";

    auto read_trim = [](const fs::path& p) -> std::string {
        std::ifstream f(p);
        if (!f.is_open())
            return {};
        std::string s;
        std::getline(f, s);
        while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) {
            s.pop_back();
        }
        return s;
    };

    std::vector<std::string> candidates;
    std::error_code ec;
    for (const auto& entry : fs::directory_iterator(kTtyClass, ec)) {
        const std::string name = entry.path().filename().string();
        if (name.rfind("ttyACM", 0) != 0)
            continue;

        // /sys/class/tty/ttyACMn/device is a symlink to the USB interface
        // (e.g. .../1-1/1-1:1.0); its parent (..) is the USB device that
        // exposes idVendor / idProduct.
        const fs::path usb_dev = entry.path() / "device" / "..";
        const std::string vid = read_trim(usb_dev / "idVendor");
        const std::string pid = read_trim(usb_dev / "idProduct");

        if (vid == kTargetVid && pid == kTargetPid) {
            const std::string dev_path = "/dev/" + name;
            RCLCPP_INFO(logger, "Discovered arm device: %s (%s:%s)", dev_path.c_str(), vid.c_str(), pid.c_str());
            return dev_path;
        }
        candidates.push_back(name + " [" + (vid.empty() ? "?" : vid) + ":" + (pid.empty() ? "?" : pid) + "]");
    }

    std::string joined;
    for (const auto& c : candidates) {
        if (!joined.empty())
            joined += ", ";
        joined += c;
    }
    RCLCPP_ERROR(logger, "No ttyACM device with VID:PID %s:%s found. Candidates: %s. Falling back to /dev/ttyACM0.",
                 kTargetVid, kTargetPid, joined.empty() ? "(none)" : joined.c_str());
    return "/dev/ttyACM0";
}

}  // namespace

MarsArmNode::MarsArmNode() : Node("mars_arm") {
    RCLCPP_INFO(this->get_logger(), "Mars Arm Node starting...");

    // Create callback groups for parallel execution
    timer_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    service_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    health_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);

    // Declare parameters
    this->declare_parameter("baud_rate", 1000000);
    this->declare_parameter("control_frequency", 100.0);
    this->declare_parameter("trajectory_rate_hz", 30.0);
    this->declare_parameter("max_jerk", 0.0);  // rad/s³, 0 = disabled
    this->declare_parameter("joints", std::vector<std::string>{});
    this->declare_parameter("gravity_compensation.enabled", false);
    this->declare_parameter("gravity_compensation.max_offset_rad", 0.25);
    this->declare_parameter("gravity_compensation.urdf_path", std::string(""));

    int baud_rate = this->get_parameter("baud_rate").as_int();
    control_frequency_ = this->get_parameter("control_frequency").as_double();
    auto joint_names_param = this->get_parameter("joints").as_string_array();

    // Load joint configurations from sub-parameters (nav2 style)
    loadJointConfigs(joint_names_param);
    setupGravityCompensation();

    // Auto-discover the arm's USB serial device (CH343 1a86:55d3)
    std::string device_name = discoverArmDevice(this->get_logger());
    RCLCPP_INFO(this->get_logger(), "Using device: %s", device_name.c_str());

    // Create Dynamixel interface
    Dynamixel::Config config;
    config.device_name = device_name;
    config.baudrate = baud_rate;
    dynamixel_ = std::make_shared<Dynamixel>(config);

    // Initialize all 7 servos
    initializeServos();

    // Create Robot for all 7 servos (IDs 1-7)
    RCLCPP_DEBUG(this->get_logger(), "Creating Robot object with servo IDs 1-7");
    std::vector<int> all_servo_ids = {1, 2, 3, 4, 5, 6, 7};
    robot_ = std::make_unique<Robot>(dynamixel_, all_servo_ids);

    // ── ARM publishers / subscribers / services ──
    RCLCPP_DEBUG(this->get_logger(), "Setting up ARM publishers/subscribers/services");
    arm_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/state", 10);
    // Latched: the standing grip target must reach subscribers that start (or
    // restart) after the last command, or they fold/seed j6 from a stale zero.
    arm_command_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/command_state",
                                                                                  rclcpp::QoS(1).transient_local());
    arm_status_pub_ = this->create_publisher<mars_msgs::msg::ArmStatus>("/mars/arm/status", 10);

    auto cmd_qos = rclcpp::QoS(1).best_effort();
    arm_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
        "/mars/arm/commands", cmd_qos, std::bind(&MarsArmNode::armCommandCallback, this, std::placeholders::_1));

    arm_torque_on_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/torque_on",
        std::bind(&MarsArmNode::armTorqueOnCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_torque_off_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/torque_off",
        std::bind(&MarsArmNode::armTorqueOffCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_reboot_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/reboot",
        std::bind(&MarsArmNode::armRebootServosCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_fix_error_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/fix_error",
        std::bind(&MarsArmNode::armFixErrorCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_service_ = this->create_service<mars_msgs::srv::GotoJS>(
        "/mars/arm/goto_js",
        std::bind(&MarsArmNode::armGotoJSCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_v2_service_ = this->create_service<mars_msgs::srv::GotoJS>(
        "/mars/arm/goto_js_v2",
        std::bind(&MarsArmNode::armGotoJSV2Callback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_traj_service_ = this->create_service<mars_msgs::srv::GotoJSTrajectory>(
        "/mars/arm/goto_js_trajectory",
        std::bind(&MarsArmNode::armGotoJSTrajectoryCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    // ── HEAD publishers / subscribers / services ──
    RCLCPP_DEBUG(this->get_logger(), "Setting up HEAD publishers/subscribers/services");
    head_position_pub_ = this->create_publisher<std_msgs::msg::String>("/mars/head/current_position", 10);
    joint_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/joint_states", 10);
    head_position_sub_ = this->create_subscription<std_msgs::msg::Int32>(
        "/mars/head/set_position", 10, std::bind(&MarsArmNode::headPositionCallback, this, std::placeholders::_1));

    head_ai_position_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/head/set_ai_position",
        std::bind(&MarsArmNode::headAiPositionCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    head_enable_service_ = this->create_service<std_srvs::srv::SetBool>(
        "/mars/head/enable_servo",
        std::bind(&MarsArmNode::headEnableServoCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    // ── Initialize messages ──
    RCLCPP_DEBUG(this->get_logger(), "Initializing joint state message with 6 joint names");
    arm_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
    joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"};

    // Initialize command buffers with current positions
    RCLCPP_DEBUG(this->get_logger(), "Initializing command buffers with current positions");
    auto [initial_positions, initial_velocities, initial_loads] = robot_->readState();
    (void)initial_loads;
    latest_head_command_ = initial_positions[6];
    syncTargetToMotorPositions();

    // ── Timers ──
    RCLCPP_DEBUG(this->get_logger(), "Creating control timer at %.1f Hz", control_frequency_);
    auto period = std::chrono::duration<double>(1.0 / control_frequency_);
    control_timer_ =
        this->create_wall_timer(std::chrono::duration_cast<std::chrono::nanoseconds>(period),
                                std::bind(&MarsArmNode::controlTimerCallback, this), timer_callback_group_);

    RCLCPP_DEBUG(this->get_logger(), "Creating health monitor timer at 0.2 Hz");
    health_timer_ = this->create_wall_timer(
        std::chrono::milliseconds(5000), std::bind(&MarsArmNode::healthMonitorCallback, this), health_callback_group_);

    // Register parameter change callback for PID hot-reload
    param_callback_handle_ =
        this->add_on_set_parameters_callback(std::bind(&MarsArmNode::onParameterChange, this, std::placeholders::_1));
    RCLCPP_DEBUG(this->get_logger(), "PID hot-reload enabled (use ros2 param set or pid_hot_reload.py)");

    RCLCPP_INFO(this->get_logger(), "Mars Arm Node ready!");

    // No homing here: the control timer consumes trajectories, and it only
    // fires once the executor spins — after this constructor returns.
}

void MarsArmNode::setupGravityCompensation() {
    const bool enabled = this->get_parameter("gravity_compensation.enabled").as_bool();
    const std::string urdf_path = this->get_parameter("gravity_compensation.urdf_path").as_string();
    if (urdf_path.empty()) {
        if (enabled) {
            throw std::runtime_error(
                "gravity_compensation.enabled but urdf_path is empty — arm.launch.py passes mars_description's "
                "mars.urdf; a bare `ros2 run mars_arm arm` must pass it too");
        }
        RCLCPP_WARN(this->get_logger(), "No gravity_compensation.urdf_path — compensation cannot be switched on");
        return;
    }
    // The URDF joints behind config joints 1-7, in chain order.
    gravity_ = std::make_unique<GravityModel>(
        urdf_path,
        std::vector<std::string>{"joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"});
    if (joint_configs_.size() != gravity_->size()) {
        throw std::runtime_error("gravity compensation needs all 7 joints configured, got " +
                                 std::to_string(joint_configs_.size()));
    }
    gravity_max_offset_rad_ = this->get_parameter("gravity_compensation.max_offset_rad").as_double();
    gravity_active_ = enabled;

    RCLCPP_INFO(this->get_logger(), "Gravity compensation %s from %s (max offset %.1f deg)",
                enabled ? "ON" : "loaded but OFF", urdf_path.c_str(),
                gravity_max_offset_rad_.load() * 180.0 / M_PI);
    for (size_t i = 0; i < joint_configs_.size(); ++i) {
        const auto& c = joint_configs_[i];
        if (c.full_pwm_torque_nm <= 0.0) {
            RCLCPP_INFO(this->get_logger(), "  joint_%zu: no full_pwm_torque_nm — not compensated", i + 1);
            continue;
        }
        const double nm_per_rad = c.kp > 0 ? 1.0 / gravityGoalOffsetRad(1.0, c.full_pwm_torque_nm, c.kp) : 0.0;
        RCLCPP_INFO(this->get_logger(), "  joint_%zu: %.2f N*m at full PWM, kp=%d -> %.1f N*m/rad", i + 1,
                    c.full_pwm_torque_nm, c.kp, nm_per_rad);
    }
}

}  // namespace mars_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<mars_arm::MarsArmNode>();

    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();

    rclcpp::shutdown();
    return 0;
}
