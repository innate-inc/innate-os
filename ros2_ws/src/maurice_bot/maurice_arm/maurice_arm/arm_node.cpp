// arm_node.cpp — Constructor, main()
#include "maurice_arm/arm_node.hpp"

namespace maurice_arm {

MauriceArmNode::MauriceArmNode() : Node("maurice_arm") {
    RCLCPP_INFO(this->get_logger(), "Maurice Arm Node starting...");

    // Create callback groups for parallel execution
    timer_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    service_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    health_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);

    // Declare parameters
    this->declare_parameter("baud_rate", 1000000);
    this->declare_parameter("control_frequency", 100.0);
    this->declare_parameter("trajectory_rate_hz", 30.0);
    this->declare_parameter("max_jerk", 0.0);  // rad/s³, 0 = disabled
    this->declare_parameter("stress_enabled", false);      // enable/disable leaky integrator
    this->declare_parameter("stress_threshold", 100.0);     // score to trigger cooldown
    this->declare_parameter("stress_cooldown_sec", 2.0);    // seconds to rest
    this->declare_parameter("stress_multiplier", 1.0);      // A in leaky integrator: A*|PWM| - C
    this->declare_parameter("stress_leak", 0.0);             // C in leaky integrator: A*|PWM| - C
    this->declare_parameter("joints", std::vector<std::string>{});

    int baud_rate = this->get_parameter("baud_rate").as_int();
    control_frequency_ = this->get_parameter("control_frequency").as_double();
    stress_enabled_ = this->get_parameter("stress_enabled").as_bool();
    stress_threshold_ = this->get_parameter("stress_threshold").as_double();
    stress_cooldown_sec_ = this->get_parameter("stress_cooldown_sec").as_double();
    stress_multiplier_ = this->get_parameter("stress_multiplier").as_double();
    stress_leak_ = this->get_parameter("stress_leak").as_double();
    auto joint_names_param = this->get_parameter("joints").as_string_array();

    // Load joint configurations from sub-parameters (nav2 style)
    loadJointConfigs(joint_names_param);

    // Use fixed device path
    std::string device_name = "/dev/ttyACM0";
    RCLCPP_INFO(this->get_logger(), "Using device: %s", device_name.c_str());

    // Create Dynamixel interface
    Dynamixel::Config config;
    config.device_name = device_name;
    config.baudrate = baud_rate;
    dynamixel_ = std::make_shared<Dynamixel>(config);

    // Initialize all 7 servos
    initializeServos();

    // Create Robot for all 7 servos (IDs 1-7)
    RCLCPP_INFO(this->get_logger(), "Creating Robot object with servo IDs 1-7");
    std::vector<int> all_servo_ids = {1, 2, 3, 4, 5, 6, 7};
    robot_ = std::make_unique<Robot>(dynamixel_, all_servo_ids);

    // ── ARM publishers / subscribers / services ──
    RCLCPP_INFO(this->get_logger(), "Setting up ARM publishers/subscribers/services");
    arm_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/state", 10);
    arm_command_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/command_state", 10);
    arm_status_pub_ = this->create_publisher<maurice_msgs::msg::ArmStatus>("/mars/arm/status", 10);

    auto cmd_qos = rclcpp::QoS(1).best_effort();
    arm_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
        "/mars/arm/commands", cmd_qos,
        std::bind(&MauriceArmNode::armCommandCallback, this, std::placeholders::_1));

    arm_torque_on_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/torque_on",
        std::bind(&MauriceArmNode::armTorqueOnCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_torque_off_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/torque_off",
        std::bind(&MauriceArmNode::armTorqueOffCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_reboot_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/reboot",
        std::bind(&MauriceArmNode::armRebootServosCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_fix_error_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/arm/fix_error",
        std::bind(&MauriceArmNode::armFixErrorCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_service_ = this->create_service<maurice_msgs::srv::GotoJS>(
        "/mars/arm/goto_js",
        std::bind(&MauriceArmNode::armGotoJSCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_v2_service_ = this->create_service<maurice_msgs::srv::GotoJS>(
        "/mars/arm/goto_js_v2",
        std::bind(&MauriceArmNode::armGotoJSV2Callback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    arm_goto_js_traj_service_ = this->create_service<maurice_msgs::srv::GotoJSTrajectory>(
        "/mars/arm/goto_js_trajectory",
        std::bind(&MauriceArmNode::armGotoJSTrajectoryCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    // ── HEAD publishers / subscribers / services ──
    RCLCPP_INFO(this->get_logger(), "Setting up HEAD publishers/subscribers/services");
    head_position_pub_ = this->create_publisher<std_msgs::msg::String>("/mars/head/current_position", 10);
    joint_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/joint_states", 10);
    head_position_sub_ = this->create_subscription<std_msgs::msg::Int32>(
        "/mars/head/set_position", 10,
        std::bind(&MauriceArmNode::headPositionCallback, this, std::placeholders::_1));

    head_ai_position_service_ = this->create_service<std_srvs::srv::Trigger>(
        "/mars/head/set_ai_position",
        std::bind(&MauriceArmNode::headAiPositionCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    head_enable_service_ = this->create_service<std_srvs::srv::SetBool>(
        "/mars/head/enable_servo",
        std::bind(&MauriceArmNode::headEnableServoCallback, this, std::placeholders::_1, std::placeholders::_2),
        rmw_qos_profile_services_default, service_callback_group_);

    // ── Initialize messages ──
    RCLCPP_INFO(this->get_logger(), "Initializing joint state message with 6 joint names");
    arm_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
    joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"};

    home_position_ = {1.445009902188274, -1.3882526130365052, 1.517106999218899,
                      0.44638840927472156, -0.08897088569736719, 0.0015339807878856412};

    // Initialize command buffers with current positions
    RCLCPP_INFO(this->get_logger(), "Initializing command buffers with current positions");
    auto [initial_positions, initial_velocities, initial_loads] = robot_->readState();
    (void)initial_loads;
    latest_arm_command_ = std::vector<int>(initial_positions.begin(), initial_positions.begin() + 6);
    latest_head_command_ = initial_positions[6];
    RCLCPP_INFO(this->get_logger(), "Command buffers initialized (arm: 6 joints, head: 1 joint)");

    // ── Timers ──
    RCLCPP_INFO(this->get_logger(), "Creating control timer at %.1f Hz", control_frequency_);
    auto period = std::chrono::duration<double>(1.0 / control_frequency_);
    control_timer_ = this->create_wall_timer(
        std::chrono::duration_cast<std::chrono::nanoseconds>(period),
        std::bind(&MauriceArmNode::controlTimerCallback, this),
        timer_callback_group_);

    RCLCPP_INFO(this->get_logger(), "Creating health monitor timer at 0.2 Hz");
    health_timer_ = this->create_wall_timer(
        std::chrono::milliseconds(5000),
        std::bind(&MauriceArmNode::healthMonitorCallback, this),
        health_callback_group_);

    // Register parameter change callback for PID hot-reload
    param_callback_handle_ = this->add_on_set_parameters_callback(
        std::bind(&MauriceArmNode::onParameterChange, this, std::placeholders::_1));
    RCLCPP_INFO(this->get_logger(), "PID hot-reload enabled (use ros2 param set or pid_hot_reload.py)");

    RCLCPP_INFO(this->get_logger(), "Maurice Arm Node ready!");

    // Go to home position on startup
    RCLCPP_INFO(this->get_logger(), "Moving to home position...");
    if (planAndExecuteTrajectory(home_position_, 5.0)) {
        RCLCPP_INFO(this->get_logger(), "Reached home position");
    } else {
        RCLCPP_WARN(this->get_logger(), "Failed to reach home position");
    }
}

} // namespace maurice_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::MauriceArmNode>();

    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();

    rclcpp::shutdown();
    return 0;
}
