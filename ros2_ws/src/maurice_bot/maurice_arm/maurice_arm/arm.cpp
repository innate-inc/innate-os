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
#include "maurice_msgs/srv/goto_js.hpp"
#include "maurice_msgs/srv/goto_js_trajectory.hpp"
#include "maurice_msgs/msg/arm_status.hpp"
#include <moveit_msgs/srv/get_motion_plan.hpp>
#include <moveit_msgs/msg/motion_plan_request.hpp>
#include <moveit_msgs/msg/robot_state.hpp>
#include <moveit_msgs/msg/constraints.hpp>
#include <moveit_msgs/msg/joint_constraint.hpp>
#include <moveit_msgs/msg/workspace_parameters.hpp>
#include <geometry_msgs/msg/pose.hpp>
#include <cmath>
#include <chrono>
#include <nlohmann/json.hpp>
#include <thread>
#include <future>
#include <sstream>
#include <cstdint>
#include <set>
#include <array>

using json = nlohmann::json;

namespace maurice_arm {

class MauriceArmNode : public rclcpp::Node {
public:
    MauriceArmNode() : Node("maurice_arm") {
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node starting...");
        
        // Create callback groups for parallel execution
        timer_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
        service_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
        health_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
        
        // Declare parameters
        this->declare_parameter("baud_rate", 1000000);
        this->declare_parameter("control_frequency", 100.0);
        this->declare_parameter("trajectory_rate_hz", 30.0);
        this->declare_parameter("joints", "{}");
        
        int baud_rate = this->get_parameter("baud_rate").as_int();
        control_frequency_ = this->get_parameter("control_frequency").as_double();
        std::string joints_str = this->get_parameter("joints").as_string();
        
        // Parse joints configuration
        parseJointConfig(joints_str);
        
        // Declare per-joint PID and profile parameters for hot-reload tuning
        for (size_t i = 0; i < joint_configs_.size(); ++i) {
            std::string prefix = "joint_" + std::to_string(i + 1) + "_";
            this->declare_parameter(prefix + "kp", joint_configs_[i].kp);
            this->declare_parameter(prefix + "ki", joint_configs_[i].ki);
            this->declare_parameter(prefix + "kd", joint_configs_[i].kd);
            this->declare_parameter(prefix + "profile_velocity", joint_configs_[i].profile_velocity);
            this->declare_parameter(prefix + "profile_acceleration", joint_configs_[i].profile_acceleration);
        }
        
        // Parse gain scheduling from JSON string parameter
        this->declare_parameter("gain_scheduling", std::string("{}"));
        parseGainScheduling(this->get_parameter("gain_scheduling").as_string());
        
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
        
        // Setup ARM publishers/subscribers/services
        RCLCPP_INFO(this->get_logger(), "Setting up ARM publishers/subscribers/services");
        arm_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/state", 10);
        arm_status_pub_ = this->create_publisher<maurice_msgs::msg::ArmStatus>("/mars/arm/status", 10);
        // Use best_effort QoS to match UDP receiver publisher for low-latency teleoperation
        auto cmd_qos = rclcpp::QoS(1).best_effort();
        arm_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
            "/mars/arm/commands", cmd_qos,
            std::bind(&MauriceArmNode::armCommandCallback, this, std::placeholders::_1));
        
        arm_torque_on_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/arm/torque_on",
            std::bind(&MauriceArmNode::armTorqueOnCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        arm_torque_off_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/arm/torque_off",
            std::bind(&MauriceArmNode::armTorqueOffCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        arm_reboot_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/arm/reboot",
            std::bind(&MauriceArmNode::armRebootServosCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        arm_goto_js_service_ = this->create_service<maurice_msgs::srv::GotoJS>(
            "/mars/arm/goto_js",
            std::bind(&MauriceArmNode::armGotoJSCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        arm_goto_js_traj_service_ = this->create_service<maurice_msgs::srv::GotoJSTrajectory>(
            "/mars/arm/goto_js_trajectory",
            std::bind(&MauriceArmNode::armGotoJSTrajectoryCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        // MoveIt planning service client
        moveit_plan_client_ = this->create_client<moveit_msgs::srv::GetMotionPlan>(
            "/plan_kinematic_path");
        
        RCLCPP_INFO(this->get_logger(), "Waiting for MoveIt planning service...");
        if (moveit_plan_client_->wait_for_service(std::chrono::seconds(5))) {
            RCLCPP_INFO(this->get_logger(), "MoveIt planning service is available");
            moveit_available_ = true;
        } else {
            RCLCPP_WARN(this->get_logger(), "MoveIt planning service not available - goto_js will fail");
            moveit_available_ = false;
        }
        
        // Setup HEAD publishers/subscribers/services
        RCLCPP_INFO(this->get_logger(), "Setting up HEAD publishers/subscribers/services");
        head_position_pub_ = this->create_publisher<std_msgs::msg::String>("/mars/head/current_position", 10);
        joint_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/joint_states", 10);
        head_position_sub_ = this->create_subscription<std_msgs::msg::Int32>(
            "/mars/head/set_position", 10,
            std::bind(&MauriceArmNode::headPositionCallback, this, std::placeholders::_1));
        
        head_ai_position_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/head/set_ai_position",
            std::bind(&MauriceArmNode::headAiPositionCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        head_enable_service_ = this->create_service<std_srvs::srv::SetBool>(
            "/mars/head/enable_servo",
            std::bind(&MauriceArmNode::headEnableServoCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        // Initialize arm joint state message (6 joints for arm state)
        RCLCPP_INFO(this->get_logger(), "Initializing joint state message with 6 joint names");
        arm_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
        
        // Initialize full joint state message (7 joints for robot_state_publisher)
        joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"};
        
        // Define home position
        home_position_ = {1.445009902188274, -1.3882526130365052, 1.517106999218899, 
                          0.44638840927472156, -0.08897088569736719, 0.0015339807878856412};
        
        // Initialize command buffers with current positions
        RCLCPP_INFO(this->get_logger(), "Initializing command buffers with current positions");
        auto [initial_positions, initial_velocities] = robot_->readState();
        latest_arm_command_ = std::vector<int>(initial_positions.begin(), initial_positions.begin() + 6);
        latest_head_command_ = initial_positions[6];
        RCLCPP_INFO(this->get_logger(), "Command buffers initialized (arm: 6 joints, head: 1 joint)");
        
        // Create timer for control loop (also handles trajectory execution)
        RCLCPP_INFO(this->get_logger(), "Creating control timer at %.1f Hz", control_frequency_);
        auto period = std::chrono::duration<double>(1.0 / control_frequency_);
        control_timer_ = this->create_wall_timer(
            std::chrono::duration_cast<std::chrono::nanoseconds>(period),
            std::bind(&MauriceArmNode::controlTimerCallback, this),
            timer_callback_group_);
        
        RCLCPP_INFO(this->get_logger(), "Creating health monitor timer at 0.2 Hz");
        auto health_period = std::chrono::milliseconds(5000);
        health_timer_ = this->create_wall_timer(
            health_period,
            std::bind(&MauriceArmNode::healthMonitorCallback, this),
            health_callback_group_);
        
        // Register parameter change callback for PID hot-reload
        param_callback_handle_ = this->add_on_set_parameters_callback(
            std::bind(&MauriceArmNode::onParameterChange, this, std::placeholders::_1));
        RCLCPP_INFO(this->get_logger(), "PID hot-reload enabled (use ros2 param set or pid_hot_reload.py)");
        
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node ready!");
        
        // Wait a bit for MoveIt to be ready, then go to home position
        RCLCPP_INFO(this->get_logger(), "Waiting 3 seconds for MoveIt to initialize...");
        std::this_thread::sleep_for(std::chrono::seconds(3));
        
        if (moveit_available_) {
            RCLCPP_INFO(this->get_logger(), "Moving to home position...");
            if (planAndExecuteTrajectory(home_position_, 5.0)) {
                RCLCPP_INFO(this->get_logger(), "Reached home position");
            } else {
                RCLCPP_WARN(this->get_logger(), "Failed to reach home position");
            }
        } else {
            RCLCPP_WARN(this->get_logger(), "MoveIt not available, skipping home position");
        }
    }
    
    ~MauriceArmNode() {
        // Timer automatically stops when destroyed
    }
    
private:
    void parseJointConfig(const std::string& json_str) {
        RCLCPP_INFO(this->get_logger(), "Parsing joint configuration...");
        auto joints = json::parse(json_str);
        
        // Parse all 7 joints (1-6 arm, 7 head)
        for (int i = 1; i <= 7; ++i) {
            std::string joint_key = "joint_" + std::to_string(i);
            if (joints.contains(joint_key)) {
                auto joint = joints[joint_key];
                JointConfig config;
                config.servo_id = joint["servo_id"];
                config.min_pos_rad = joint["position_limits"]["min"];
                config.max_pos_rad = joint["position_limits"]["max"];
                config.pwm_limit = joint["pwm_limits"];
                config.control_mode = joint["control_mode"];
                
                RCLCPP_INFO(this->get_logger(), "Joint %d: servo_id=%d, limits=[%.3f, %.3f] rad, pwm=%d, mode=%d",
                    i, config.servo_id, config.min_pos_rad, config.max_pos_rad, config.pwm_limit, config.control_mode);
                
                if (joint.contains("current_limit")) {
                    config.current_limit = joint["current_limit"];
                    RCLCPP_INFO(this->get_logger(), "  Current limit: %d", config.current_limit);
                }
                
                if (joint.contains("homing_offset")) {
                    config.homing_offset = joint["homing_offset"];
                    RCLCPP_INFO(this->get_logger(), "  Homing offset: %d", config.homing_offset);
                }
                
                config.kp = joint["pid_gains"]["kp"];
                config.ki = joint["pid_gains"]["ki"];
                config.kd = joint["pid_gains"]["kd"];
                RCLCPP_INFO(this->get_logger(), "  PID gains: kp=%d, ki=%d, kd=%d", config.kp, config.ki, config.kd);
                
                if (joint.contains("profile_velocity")) {
                    config.profile_velocity = joint["profile_velocity"];
                    RCLCPP_INFO(this->get_logger(), "  Profile velocity: %d", config.profile_velocity);
                }
                if (joint.contains("profile_acceleration")) {
                    config.profile_acceleration = joint["profile_acceleration"];
                    RCLCPP_INFO(this->get_logger(), "  Profile acceleration: %d", config.profile_acceleration);
                }
                
                // Parse head-specific config for joint 7
                // Head angle limits are derived from position_limits (not duplicated in config)
                if (i == 7 && joint.contains("head_config")) {
                    auto head = joint["head_config"];
                    config.head_ai_position_deg = head["ai_position_deg"];
                    config.head_direction_reversed = head["direction_reversed"];
                    
                    // Compute head angle limits from position_limits (accounting for direction reversal)
                    constexpr double RAD_TO_DEG = 180.0 / M_PI;
                    if (config.head_direction_reversed) {
                        config.head_min_angle_deg = -config.max_pos_rad * RAD_TO_DEG;
                        config.head_max_angle_deg = -config.min_pos_rad * RAD_TO_DEG;
                    } else {
                        config.head_min_angle_deg = config.min_pos_rad * RAD_TO_DEG;
                        config.head_max_angle_deg = config.max_pos_rad * RAD_TO_DEG;
                    }
                    
                    RCLCPP_INFO(this->get_logger(), "  Head config: range=[%.1f, %.1f] deg (from position_limits), AI pos=%.1f deg, reversed=%s",
                        config.head_min_angle_deg, config.head_max_angle_deg, config.head_ai_position_deg,
                        config.head_direction_reversed ? "true" : "false");
                }
                
                joint_configs_.push_back(config);
            }
        }
        RCLCPP_INFO(this->get_logger(), "Parsed %zu joint configurations", joint_configs_.size());
    }
    
    void parseGainScheduling(const std::string& json_str) {
        try {
            auto gs = json::parse(json_str);
            gs_enabled_ = gs.value("enabled", false);
            
            // Initialize with current joint defaults
            for (int i = 0; i < 3; i++) {
                auto& jc = joint_configs_[i + 1];  // joints 2, 3, 4
                gs_near_[i] = {jc.kp, jc.ki, jc.kd};
                gs_far_[i] = {jc.kp, jc.ki, jc.kd};
            }
            
            const std::array<std::string, 2> profiles = {"near", "far"};
            for (const auto& profile : profiles) {
                if (!gs.contains(profile)) continue;
                auto& arr = (profile == "near") ? gs_near_ : gs_far_;
                for (int j : {2, 3, 4}) {
                    std::string key = "joint_" + std::to_string(j);
                    if (!gs[profile].contains(key)) continue;
                    auto& jg = gs[profile][key];
                    int idx = j - 2;
                    arr[idx].kp = jg.value("kp", arr[idx].kp);
                    arr[idx].ki = jg.value("ki", arr[idx].ki);
                    arr[idx].kd = jg.value("kd", arr[idx].kd);
                }
            }
            
            RCLCPP_INFO(this->get_logger(), "Gain scheduling %s | near: J2[%d,%d,%d] J3[%d,%d,%d] J4[%d,%d,%d] | far: J2[%d,%d,%d] J3[%d,%d,%d] J4[%d,%d,%d]",
                gs_enabled_ ? "ENABLED" : "DISABLED",
                gs_near_[0].kp, gs_near_[0].ki, gs_near_[0].kd,
                gs_near_[1].kp, gs_near_[1].ki, gs_near_[1].kd,
                gs_near_[2].kp, gs_near_[2].ki, gs_near_[2].kd,
                gs_far_[0].kp, gs_far_[0].ki, gs_far_[0].kd,
                gs_far_[1].kp, gs_far_[1].ki, gs_far_[1].kd,
                gs_far_[2].kp, gs_far_[2].ki, gs_far_[2].kd);
        } catch (const json::exception& e) {
            RCLCPP_WARN(this->get_logger(), "Failed to parse gain_scheduling JSON: %s", e.what());
        }
    }
    
    rcl_interfaces::msg::SetParametersResult onParameterChange(
        const std::vector<rclcpp::Parameter>& parameters) {
        
        rcl_interfaces::msg::SetParametersResult result;
        result.successful = true;
        
        // Collect which joints had PID or profile changes
        std::set<int> pid_changed_joints;
        std::set<int> profile_changed_joints;
        
        for (const auto& param : parameters) {
            std::string name = param.get_name();
            
            // Handle gain scheduling JSON string
            if (name == "gain_scheduling") {
                parseGainScheduling(param.as_string());
                continue;
            }
            
            // Match pattern: joint_N_<suffix>
            if (name.size() >= 10 && name.substr(0, 6) == "joint_") {
                size_t underscore2 = name.find('_', 6);
                if (underscore2 == std::string::npos) continue;
                
                int joint_num = 0;
                try {
                    joint_num = std::stoi(name.substr(6, underscore2 - 6));
                } catch (...) { continue; }
                
                if (joint_num < 1 || joint_num > static_cast<int>(joint_configs_.size())) continue;
                
                std::string suffix = name.substr(underscore2 + 1);
                int value = static_cast<int>(param.as_int());
                
                if (suffix == "kp") {
                    joint_configs_[joint_num - 1].kp = value;
                    pid_changed_joints.insert(joint_num);
                } else if (suffix == "ki") {
                    joint_configs_[joint_num - 1].ki = value;
                    pid_changed_joints.insert(joint_num);
                } else if (suffix == "kd") {
                    joint_configs_[joint_num - 1].kd = value;
                    pid_changed_joints.insert(joint_num);
                } else if (suffix == "profile_velocity") {
                    joint_configs_[joint_num - 1].profile_velocity = value;
                    profile_changed_joints.insert(joint_num);
                } else if (suffix == "profile_acceleration") {
                    joint_configs_[joint_num - 1].profile_acceleration = value;
                    profile_changed_joints.insert(joint_num);
                } else {
                    continue;
                }
                
                RCLCPP_INFO(this->get_logger(), "Hot-reload: joint %d %s = %d",
                    joint_num, suffix.c_str(), value);
            }
        }
        
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            // SyncWrite PID for changed joints
            if (!pid_changed_joints.empty()) {
                std::vector<std::tuple<int, int, int, int>> pid_data;
                for (int jn : pid_changed_joints) {
                    const auto& c = joint_configs_[jn - 1];
                    pid_data.emplace_back(c.servo_id, c.kd, c.ki, c.kp);
                }
                dynamixel_->syncWritePID(pid_data);
                RCLCPP_INFO(this->get_logger(), "Hot-reload: sync-wrote PID for %zu servo(s)", pid_data.size());
            }
            
            // SyncWrite profile for changed joints
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
    
    void initializeServos() {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);
        configureServosLocked();
    }

    void configureServosLocked(bool enable_torque = true) {
        RCLCPP_INFO(this->get_logger(), "Configuring all 7 servos...");
        
        // Configure all servos (IDs 1-7) uniformly
        for (const auto& config : joint_configs_) {
            RCLCPP_INFO(this->get_logger(), "Configuring servo %d", config.servo_id);
            
            RCLCPP_INFO(this->get_logger(), "  Disabling torque on servo %d", config.servo_id);
            dynamixel_->disableTorque(config.servo_id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set position limits
            int min_encoder = static_cast<int>((config.min_pos_rad / (2 * M_PI)) * 4096 + 2048);
            int max_encoder = static_cast<int>((config.max_pos_rad / (2 * M_PI)) * 4096 + 2048);
            RCLCPP_INFO(this->get_logger(), "  Setting position limits: [%d, %d] (encoder)", min_encoder, max_encoder);
            dynamixel_->setMinPositionLimit(config.servo_id, min_encoder);
            dynamixel_->setMaxPositionLimit(config.servo_id, max_encoder);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set PWM limit
            RCLCPP_INFO(this->get_logger(), "  Setting PWM limit: %d", config.pwm_limit);
            dynamixel_->setPwmLimit(config.servo_id, config.pwm_limit);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set current limit if specified
            if (config.current_limit > 0) {
                RCLCPP_INFO(this->get_logger(), "  Setting current limit: %d", config.current_limit);
                dynamixel_->setCurrentLimit(config.servo_id, config.current_limit);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            
            // Set operating mode
            OperatingMode mode = OperatingMode::POSITION;
            const char* mode_name = "POSITION";
            if (config.control_mode == 1) { mode = OperatingMode::VELOCITY; mode_name = "VELOCITY"; }
            else if (config.control_mode == 3) { mode = OperatingMode::POSITION; mode_name = "POSITION"; }
            else if (config.control_mode == 5) { mode = OperatingMode::CURRENT_CONTROLLED_POSITION; mode_name = "CURRENT_CONTROLLED_POSITION"; }
            else if (config.control_mode == 16) { mode = OperatingMode::PWM; mode_name = "PWM"; }
            
            RCLCPP_INFO(this->get_logger(), "  Setting operating mode: %s (%d)", mode_name, config.control_mode);
            dynamixel_->setOperatingMode(config.servo_id, mode);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set homing offset if specified (for head servo)
            if (config.homing_offset != 0) {
                RCLCPP_INFO(this->get_logger(), "  Setting homing offset: %d", config.homing_offset);
                dynamixel_->setHomeOffset(config.servo_id, config.homing_offset);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            
            // PID gains are set via syncWritePID after this loop
            RCLCPP_INFO(this->get_logger(), "  PID gains: P=%d, I=%d, D=%d (will sync-write)", config.kp, config.ki, config.kd);
            
            // Set profile velocity and acceleration (0 = no limit)
            if (config.profile_velocity > 0) {
                RCLCPP_INFO(this->get_logger(), "  Setting profile velocity: %d", config.profile_velocity);
                dynamixel_->setProfileVelocity(config.servo_id, config.profile_velocity);
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            if (config.profile_acceleration > 0) {
                RCLCPP_INFO(this->get_logger(), "  Setting profile acceleration: %d", config.profile_acceleration);
                dynamixel_->setProfileAcceleration(config.servo_id, config.profile_acceleration);
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            
            // Enable torque (if requested)
            if (enable_torque) {
                RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", config.servo_id);
                dynamixel_->enableTorque(config.servo_id);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
        }
        
        // Write all PID gains in a single SyncWrite packet
        std::vector<std::tuple<int, int, int, int>> pid_data;
        for (const auto& config : joint_configs_) {
            pid_data.emplace_back(config.servo_id, config.kd, config.ki, config.kp);
        }
        RCLCPP_INFO(this->get_logger(), "SyncWrite PID gains for %zu servos", pid_data.size());
        dynamixel_->syncWritePID(pid_data);
        
        // Move head to default position
        RCLCPP_INFO(this->get_logger(), "Moving head to default position (0.0 deg)");
        moveHeadToAngleLocked(0.0);
    }
    
    // Timer callback for unified control loop (replaces thread-based loop)
    void controlTimerCallback() {
        auto loop_start = std::chrono::steady_clock::now();
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            // ========== READ STATE ==========
            auto [positions, velocities] = robot_->readState();
            
            // Read motor load/effort for all 7 servos
            std::vector<double> efforts;
            for (int id = 1; id <= 7; ++id) {
                int16_t load = dynamixel_->readPresentLoad(id);
                // Convert load to percentage (-100% to 100%)
                efforts.push_back(static_cast<double>(load) / 10.0);
            }
            
            // ========== PUBLISH ARM STATE ==========
            // Convert to radians
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
            
            // ========== PUBLISH HEAD POSITION ==========
            int head_encoder = positions[6];  // Index 6 = servo 7
            publishHeadPosition(head_encoder);
            
            // ========== PUBLISH FULL JOINT STATE (all 7 joints) to /joint_states ==========
            // Convert head position to radians with direction reversal
            double head_angle_rad = positions_rad[6];
            if (joint_configs_[6].head_direction_reversed) {
                head_angle_rad = -head_angle_rad;
            }
            
            // Build full 7-joint state for robot_state_publisher
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
            joint_state_pub_->publish(joint_state_msg_);    // /joint_states (for robot_state_publisher)
            
            // ========== GAIN SCHEDULING ==========
            if (gs_enabled_ && ++gs_cycle_counter_ >= kGainScheduleInterval) {
                gs_cycle_counter_ = 0;
                
                // Compute extension via 2D planar FK (shoulder XZ plane, Y-axis pitch joints)
                // Link offsets from URDF (joint-to-joint in parent frame XZ)
                constexpr double L2_x = 0.02825, L2_z = 0.12125;  // joint2 → joint3
                constexpr double L3_x = 0.1375,  L3_z = 0.0045;   // joint3 → joint4
                constexpr double L45_x = 0.110838;                  // joint4 → EE (joint5 is X-roll, no XZ effect)
                constexpr double kMaxReach = 0.37291;               // sum of link lengths
                
                double q2 = positions_rad[1], q3 = positions_rad[2], q4 = positions_rad[3];
                double a2 = q2, a23 = q2 + q3, a234 = q2 + q3 + q4;
                
                double ee_x = L2_x*std::cos(a2)  + L2_z*std::sin(a2)
                            + L3_x*std::cos(a23) + L3_z*std::sin(a23)
                            + L45_x*std::cos(a234);
                
                double horiz_reach = std::abs(ee_x);  // horizontal projection — what actually creates gravity torque
                double extension_linear = std::clamp((horiz_reach / kMaxReach - 0.1) / 0.9, 0.0, 1.0);
                double extension = extension_linear * extension_linear;  // quadratic: stays near 'near' gains longer
                
                bool gs_changed = false;
                std::vector<std::tuple<int, int, int, int>> gs_pid_data;
                
                for (int i = 0; i < 3; i++) {
                    int ji = i + 1;  // joint_configs_ index (0-based): joints 2,3,4 = indices 1,2,3
                    GainProfile interp;
                    interp.kp = gs_near_[i].kp + static_cast<int>(extension * (gs_far_[i].kp - gs_near_[i].kp));
                    interp.ki = gs_near_[i].ki + static_cast<int>(extension * (gs_far_[i].ki - gs_near_[i].ki));
                    interp.kd = gs_near_[i].kd + static_cast<int>(extension * (gs_far_[i].kd - gs_near_[i].kd));
                    
                    if (interp.kp != gs_last_applied_[i].kp ||
                        interp.ki != gs_last_applied_[i].ki ||
                        interp.kd != gs_last_applied_[i].kd) {
                        gs_changed = true;
                        gs_last_applied_[i] = interp;
                        joint_configs_[ji].kp = interp.kp;
                        joint_configs_[ji].ki = interp.ki;
                        joint_configs_[ji].kd = interp.kd;
                    }
                    gs_pid_data.emplace_back(joint_configs_[ji].servo_id, interp.kd, interp.ki, interp.kp);
                }
                if (gs_changed) {
                    auto pid_start = std::chrono::steady_clock::now();
                    dynamixel_->syncWritePID(gs_pid_data);
                    auto pid_us = std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - pid_start).count();
                    gs_pid_write_us_sum_ += pid_us;
                    gs_pid_write_count_++;
                    RCLCPP_INFO(this->get_logger(),
                        "GainSched ext=%.2f (h=%.3fm) | J2 P=%d I=%d D=%d | J3 P=%d I=%d D=%d | J4 P=%d I=%d D=%d",
                        extension, horiz_reach,
                        gs_last_applied_[0].kp, gs_last_applied_[0].ki, gs_last_applied_[0].kd,
                        gs_last_applied_[1].kp, gs_last_applied_[1].ki, gs_last_applied_[1].kd,
                        gs_last_applied_[2].kp, gs_last_applied_[2].ki, gs_last_applied_[2].kd);
                }
            }
            
            // ========== SEND COMMANDS IF AVAILABLE ==========
            if (has_arm_command_.load() || has_head_command_.load()) {
                // Assemble full 7-servo command from latest commanded values
                std::vector<int> full_command(7);
                
                // Get 6 arm positions from latest arm command
                {
                    std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
                    std::copy(latest_arm_command_.begin(), latest_arm_command_.end(), full_command.begin());
                    has_arm_command_ = false;
                }
                
                // Get 1 head position from latest head command
                {
                    std::lock_guard<std::mutex> head_lock(head_command_mutex_);
                    full_command[6] = latest_head_command_;
                    has_head_command_ = false;
                }
                
                // Send the combined command
                robot_->setGoalPos(full_command);
            }
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Control timer error: %s", e.what());
        }
        auto loop_us = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - loop_start).count();
        loop_us_sum_ += loop_us;
        loop_us_max_ = std::max(loop_us_max_, loop_us);
        loop_timing_count_++;
        if (loop_timing_count_ >= 200) {  // log every ~2s at 100Hz
            long avg_loop = loop_us_sum_ / loop_timing_count_;
            long avg_pid = gs_pid_write_count_ > 0 ? gs_pid_write_us_sum_ / gs_pid_write_count_ : 0;
            RCLCPP_INFO(this->get_logger(),
                "LoopTiming: avg=%ldus max=%ldus | syncWritePID: avg=%ldus (%ld calls)",
                avg_loop, loop_us_max_, avg_pid, gs_pid_write_count_);
            loop_us_sum_ = 0; loop_us_max_ = 0; loop_timing_count_ = 0;
            gs_pid_write_us_sum_ = 0; gs_pid_write_count_ = 0;
        }
    }
    
    void healthMonitorCallback() {
        maurice_msgs::msg::ArmStatus status_msg;
        status_msg.is_ok = true;
        status_msg.error = "All servos nominal";
        status_msg.is_torque_enabled = arm_torque_enabled_.load();
        
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            for (const auto& config : joint_configs_) {
                const int servo_id = config.servo_id;
                
                uint8_t hw_status = dynamixel_->readHardwareErrorStatus(servo_id);
                if (hw_status != 0) {
                    status_msg.is_ok = false;
                    status_msg.error = describeHardwareError(hw_status, servo_id);
                    break;
                }
                
                int16_t present_load = dynamixel_->readPresentLoad(servo_id);
                if (std::abs(static_cast<int>(present_load)) >= kLoadWarningThreshold) {
                    status_msg.is_ok = false;
                    double load_percent = static_cast<double>(present_load) / 10.0;
                    status_msg.error = "Servo " + std::to_string(servo_id) + 
                        " high load (" + std::to_string(load_percent) + "%)";
                    break;
                }
                
                uint8_t temperature = dynamixel_->readPresentTemperature(servo_id);
                if (temperature >= kTemperatureWarningC) {
                    status_msg.is_ok = false;
                    status_msg.error = "Servo " + std::to_string(servo_id) + 
                        " high temperature (" + std::to_string(static_cast<int>(temperature)) + " C)";
                    break;
                }
            }
        } catch (const std::exception& e) {
            status_msg.is_ok = false;
            status_msg.error = std::string("Health check error: ") + e.what();
        }
        
        arm_status_pub_->publish(status_msg);
        
        if (status_msg.is_ok != last_arm_status_.is_ok || status_msg.error != last_arm_status_.error) {
            if (status_msg.is_ok) {
                RCLCPP_INFO(this->get_logger(), "Arm health nominal: %s", status_msg.error.c_str());
            } else {
                RCLCPP_ERROR(this->get_logger(), "Arm health issue: %s", status_msg.error.c_str());
            }
            last_arm_status_ = status_msg;
        }
    }
    
    std::string describeHardwareError(uint8_t status, int servo_id) const {
        std::vector<std::string> error_bits;
        if (status & 0x20) { error_bits.emplace_back("overload"); }
        if (status & 0x10) { error_bits.emplace_back("electrical fault"); }
        if (status & 0x08) { error_bits.emplace_back("encoder fault"); }
        if (status & 0x04) { error_bits.emplace_back("overheating"); }
        if (status & 0x01) { error_bits.emplace_back("input voltage"); }
        
        std::ostringstream oss;
        oss << "Servo " << servo_id << " hardware error";
        if (!error_bits.empty()) {
            oss << ": ";
            for (size_t i = 0; i < error_bits.size(); ++i) {
                if (i > 0) {
                    oss << ", ";
                }
                oss << error_bits[i];
            }
        } else {
            oss << " (code 0x" << std::hex << static_cast<int>(status) << ")";
        }
        return oss.str();
    }
    
    
    /**
     * Shared helper: apply intelligent joint limits, direction flips, and
     * convert from external radians to hardware encoder counts.
     *
     * @param command_data  Joint positions in external (radian) convention.
     *                      Modified in-place (limits + flips applied).
     * @return Encoder counts ready to write to servos.
     */
    std::vector<int> applyLimitsAndConvertToEncoder(std::vector<double>& command_data) {
        // ===== INTELLIGENT JOINT LIMITS =====
        // Adjust joint2 limits based on joint1 position (collision avoidance)
        if (command_data.size() >= 2) {
            double joint1_pos = command_data[0];  // joint1 position in radians
            double joint2_pos = command_data[1];  // joint2 position in radians (before flipping)
            
            // Get joint2 limits from config (index 1 = joint_2)
            const auto& joint2_config = joint_configs_[1];
            double config_min = joint2_config.min_pos_rad;  // e.g., -1.5708
            double config_max = joint2_config.max_pos_rad;  // e.g., 1.22
            
            // Since joint2 will be flipped, we need to invert the limits for pre-flip values
            // After flip: joint2_flipped = -joint2_pos
            // So if we want joint2_flipped to be within [config_min, config_max]
            // We need joint2_pos to be within [-config_max, -config_min]
            double joint2_min_limit = -config_max;  // Will become config_max after flip
            double joint2_max_limit = -config_min;  // Will become config_min after flip
            
            // Determine joint2's minimum limit based on joint1's position with linear slope
            const double original_min_limit = -config_max;
            const double restricted_limit = -0.5;  // Restricted limit (pre-flip)
            
            if (joint1_pos < -1.35) {
                // Below -0.25 rad: no additional restriction (negative side clear)
            } else if (joint1_pos < -1.0) {
                // Between -0.25 and 0.0 rad: linear interpolation (entering restricted zone)
                double t = - (joint1_pos - (-1.0)) / (-1.0 - (-1.35));  // 0 at -0.25, 1 at 0.0
                double interpolated_limit = restricted_limit + t * (original_min_limit - restricted_limit);
                joint2_min_limit = std::max(joint2_min_limit, interpolated_limit);
            } else
            if (joint1_pos < 1.0) {
                // Below 1.0 rad: fully restricted to -0.5
                joint2_min_limit = std::max(joint2_min_limit, restricted_limit);
            } else if (joint1_pos < 1.25) {
                // Between 1.0 and 1.25 rad: linear interpolation
                double t = (joint1_pos - 1.0) / (1.25 - 1.0);  // 0 at 1.0, 1 at 1.25
                double interpolated_limit = restricted_limit + t * (original_min_limit - restricted_limit);
                joint2_min_limit = std::max(joint2_min_limit, interpolated_limit);
            }
            // Above 1.25 rad: use original config limits (no additional restriction)
            
            // Warn if clamping occurs
            if (joint2_pos < joint2_min_limit) {
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
                    "Joint2 limited due to joint1=%.3f: requested %.3f, clamped to %.3f", 
                    joint1_pos, joint2_pos, joint2_min_limit);
            }
            
            // Enforce the limits (before flipping)
            command_data[1] = std::clamp(joint2_pos, joint2_min_limit, joint2_max_limit);
        }
        
        // Now convert from EXTERNAL to HARDWARE convention for servos
        // Apply direction flips for joints 2, 3, 4, 6 (indices 1, 2, 3, 5)
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
    
    void armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg) {
        try {
            // Commands come in EXTERNAL convention (same as published joint states)
            std::vector<double> command_data(msg->data.begin(), msg->data.end());
            
            // Validate that we have exactly 6 arm joints
            if (command_data.size() != 6) {
                RCLCPP_ERROR(this->get_logger(), "Action size must match number of servos. Expected 6, got %zu", command_data.size());
                return;
            }
            
            std::vector<int> command_encoder = applyLimitsAndConvertToEncoder(command_data);
            
            // Store only the 6 arm positions - timer will add head position
            std::lock_guard<std::mutex> lock(arm_command_mutex_);
            latest_arm_command_ = command_encoder;
            has_arm_command_ = true;
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in arm command callback: %s", e.what());
        }
    }
    
    void armTorqueOnCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/torque_on");
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            for (int id = 1; id <= 6; ++id) {
                RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", id);
                dynamixel_->enableTorque(id);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            arm_torque_enabled_ = true;
            response->success = true;
            response->message = "Enabled torque for all arm servos";
            RCLCPP_INFO(this->get_logger(), "Successfully enabled torque for all arm servos");
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "Failed to enable torque: %s", e.what());
        }
    }
    
    void armTorqueOffCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/torque_off");
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            for (int id = 1; id <= 6; ++id) {
                RCLCPP_INFO(this->get_logger(), "  Disabling torque on servo %d", id);
                dynamixel_->disableTorque(id);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            arm_torque_enabled_ = false;
            response->success = true;
            response->message = "Disabled torque for all arm servos";
            RCLCPP_INFO(this->get_logger(), "Successfully disabled torque for all arm servos");
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "Failed to disable torque: %s", e.what());
        }
    }
    
    void armRebootServosCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/reboot");
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            RCLCPP_INFO(this->get_logger(), "Rebooting all servos (IDs 1-7)");
            robot_->rebootAllServos();
            
            RCLCPP_INFO(this->get_logger(), "Reapplying configuration after reboot (arm torque off)");
            configureServosLocked(false);
            
            // Re-enable torque only for head servo
            RCLCPP_INFO(this->get_logger(), "Enabling torque on head servo (ID 7)");
            dynamixel_->enableTorque(7);
            
            arm_torque_enabled_ = false;
            response->success = true;
            response->message = "Rebooted and reinitialized all servos (arm torque off, head torque on)";
            RCLCPP_INFO(this->get_logger(), "Successfully rebooted and reinitialized all servos");
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "Failed to reboot servos: %s", e.what());
        }
    }
    
    // HEAD control methods
    int logicalAngleToEncoder(double logical_angle_deg) {
        // Get head config (servo 7)
        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
        
        // Reverse direction if configured
        double angle_deg = head_config.head_direction_reversed ? -logical_angle_deg : logical_angle_deg;
        double angle_rad = angle_deg * M_PI / 180.0;
        int encoder_value = static_cast<int>((angle_rad / (2 * M_PI)) * 4096 + 2048);
        return encoder_value;
    }
    
    double encoderToLogicalAngle(int encoder_value) {
        // Get head config (servo 7)
        const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
        
        double angle_rad = (encoder_value - 2048) * (2 * M_PI) / 4096.0;
        double servo_angle_deg = angle_rad * 180.0 / M_PI;
        
        // Reverse direction if configured
        double logical_angle = head_config.head_direction_reversed ? -servo_angle_deg : servo_angle_deg;
        return logical_angle;
    }
    
    void moveHeadToAngle(double logical_angle_deg) {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);
        moveHeadToAngleLocked(logical_angle_deg);
    }
    
    void moveHeadToAngleLocked(double logical_angle_deg) {
        int encoder_value = logicalAngleToEncoder(logical_angle_deg);
        dynamixel_->setGoalPosition(7, encoder_value);
    }
    
    void publishHeadPosition(int encoder_value) {
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
    
    void headPositionCallback(const std_msgs::msg::Int32::SharedPtr msg) {
        try {
            double logical_position = static_cast<double>(msg->data);
            
            const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
            
            if (logical_position < head_config.head_min_angle_deg || 
                logical_position > head_config.head_max_angle_deg) {
                RCLCPP_ERROR(this->get_logger(), "Head position %f out of range [%f, %f]", 
                    logical_position, head_config.head_min_angle_deg, head_config.head_max_angle_deg);
                return;
            }
            
            int head_goal_encoder = logicalAngleToEncoder(logical_position);
            
            std::lock_guard<std::mutex> lock(head_command_mutex_);
            latest_head_command_ = head_goal_encoder;
            has_head_command_ = true;
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in head position callback: %s", e.what());
        }
    }
    
    void headAiPositionCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        try {
            // Get head config for AI position
            const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
            
            RCLCPP_INFO(this->get_logger(), "Moving head to AI position (%f deg)", 
                head_config.head_ai_position_deg);
            
            int head_goal_encoder = logicalAngleToEncoder(head_config.head_ai_position_deg);
            
            std::lock_guard<std::mutex> lock(head_command_mutex_);
            latest_head_command_ = head_goal_encoder;
            has_head_command_ = true;
            
            response->success = true;
            response->message = "Head moving to AI position";
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in head AI position callback: %s", e.what());
            response->success = false;
            response->message = e.what();
        }
    }
    
    void headEnableServoCallback(
        const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
        std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
        RCLCPP_INFO(this->get_logger(), "Service called: /mars/head/enable_servo (enable=%s)", request->data ? "true" : "false");
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
            RCLCPP_ERROR(this->get_logger(), "Failed to %s head servo: %s", 
                request->data ? "enable" : "disable", e.what());
        }
    }
    
    // ========== TRAJECTORY PLANNING AND EXECUTION ==========
    
    /**
     * Call MoveIt to plan a collision-free trajectory.
     * Returns waypoints and time_from_start if successful.
     * Note: Plans for 6 DOF arm (joint1-6) including gripper.
     */
    std::pair<std::vector<std::vector<double>>, std::vector<double>> 
    planWithMoveIt(const std::vector<double>& start, const std::vector<double>& goal, double planning_time) {
        
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
        request->motion_plan_request.planner_id = "RRTConnect";  // or "chomp"
        
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
    
    /**
     * Interpolate MoveIt trajectory waypoints to 30 Hz.
     * Note: Waypoints are 6 joints (including gripper).
     */
    std::vector<std::vector<double>> interpolateMoveItTrajectory(
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
                // At or past last waypoint - add it and finish
                trajectory.push_back(waypoints.back());
                break;  // Don't keep adding the last point
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
    
    /**
     * Compute a cubic (third-order) spline trajectory between start and goal.
     * Uses smooth acceleration/deceleration profile: ratio = 3*(t/T)^2 - 2*(t/T)^3
     * 
     * @param start Starting joint positions in radians
     * @param goal Goal joint positions in radians
     * @param duration Total trajectory duration in seconds
     * @param dt Time step between trajectory points (e.g., 1/30 for 30 Hz)
     * @return Vector of trajectory points (each point is a vector of joint positions)
     */
    std::vector<std::vector<double>> computeCubicSplineTrajectory(
        const std::vector<double>& start,
        const std::vector<double>& goal,
        double duration,
        double dt) {
        
        std::vector<std::vector<double>> trajectory;
        
        if (start.size() != goal.size() || start.empty()) {
            RCLCPP_ERROR(this->get_logger(), "Invalid start/goal sizes for spline trajectory");
            return trajectory;
        }
        
        int num_steps = static_cast<int>(duration / dt);
        if (num_steps < 1) num_steps = 1;
        
        for (int step = 0; step <= num_steps; ++step) {
            double t = step * dt;
            double t_ratio = t / duration;
            // Cubic spline interpolation: smooth acceleration and deceleration
            double ratio = 3.0 * t_ratio * t_ratio - 2.0 * t_ratio * t_ratio * t_ratio;
            
            std::vector<double> point(start.size());
            for (size_t j = 0; j < start.size(); ++j) {
                point[j] = start[j] + (goal[j] - start[j]) * ratio;
            }
            trajectory.push_back(point);
        }
        
        return trajectory;
    }
    
    /**
     * Internal function to plan and execute a trajectory to a target joint state.
     * This can be called from the service callback or any other part of the code.
     * Uses simple cubic spline planning as primary method (fast, no collision checking).
     * MoveIt planning code is retained but not used by default.
     * 
     * @param target_positions Target joint positions in radians (6 joints including gripper)
     * @param trajectory_time Total time for trajectory execution in seconds
     * @return true if planning succeeded, false otherwise
     */
    bool planAndExecuteTrajectory(const std::vector<double>& target_positions, double trajectory_time) {
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
        
        RCLCPP_INFO(this->get_logger(), "Computed cubic spline trajectory with %zu points over %.2fs",
                    interpolated_trajectory.size(), trajectory_time);
        
        RCLCPP_INFO(this->get_logger(), "Executing trajectory with %zu waypoints over %.2f seconds", 
                    interpolated_trajectory.size(), trajectory_time);
        
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
    
    /**
     * Plan and execute a multi-waypoint trajectory.
     * Linearly interpolates between consecutive waypoints at trajectory_rate_hz
     * with no deceleration/acceleration at intermediate waypoints.
     *
     * @param waypoints Vector of joint-position vectors (6 joints each)
     * @param segment_durations Duration in seconds for each segment (size = waypoints.size()-1)
     * @return true if execution succeeded
     */
    bool planAndExecuteMultiWaypointTrajectory(
        const std::vector<std::vector<double>>& waypoints,
        const std::vector<double>& segment_durations) {
        
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
    
    /**
     * Service callback for goto_js_trajectory service.
     * Unpacks waypoints from flat array and executes multi-waypoint trajectory.
     */
    void armGotoJSTrajectoryCallback(
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
                // First segment: use the first segment_duration for getting to the first requested waypoint
                // If no segment duration provided for this, use 0.5s default
                if (!durations.empty()) {
                    durations.insert(durations.begin(), durations[0]);
                } else {
                    durations.insert(durations.begin(), 0.5);
                }
            }
        }
        
        response->success = planAndExecuteMultiWaypointTrajectory(waypoints, durations);
    }
    
    /**
     * Service callback for goto_js service.
     * Calls the internal planning function.
     * Plans for 6 joints (joint1-6) including gripper.
     */
    void armGotoJSCallback(
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
    
    struct JointConfig {
        int servo_id;
        double min_pos_rad;
        double max_pos_rad;
        int pwm_limit;
        int current_limit = 0;
        int homing_offset = 0;
        int control_mode;
        int kp, ki, kd;
        int profile_velocity = 0;
        int profile_acceleration = 0;
        // Head-specific fields (for joint 7)
        // Note: head angle limits are derived from min_pos_rad/max_pos_rad at startup
        // Head position is inverted: negative head angle = positive servo angle
        double head_min_angle_deg = 0.0;  // Computed from position_limits
        double head_max_angle_deg = 0.0;  // Computed from position_limits
        double head_ai_position_deg = 0.0;
        bool head_direction_reversed = false;
    };
    
    std::shared_ptr<Dynamixel> dynamixel_;
    std::unique_ptr<Robot> robot_;
    std::vector<JointConfig> joint_configs_;
    
    // ARM members
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr arm_state_pub_;
    rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr arm_command_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_on_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_off_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_reboot_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJS>::SharedPtr arm_goto_js_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJSTrajectory>::SharedPtr arm_goto_js_traj_service_;
    sensor_msgs::msg::JointState arm_state_msg_;  // 6-joint message for /mars/arm/state
    sensor_msgs::msg::JointState joint_state_msg_;  // 7-joint message for /joint_states
    std::vector<int> latest_arm_command_;
    std::mutex arm_command_mutex_;
    std::atomic<bool> has_arm_command_{false};
    
    
    // Joint state tracking for planning
    std::vector<double> latest_joint_positions_;
    std::mutex joint_state_mutex_;
    
    // MoveIt planning (6 DOF arm including gripper joint6)
    rclcpp::Client<moveit_msgs::srv::GetMotionPlan>::SharedPtr moveit_plan_client_;
    bool moveit_available_{false};
    std::vector<std::string> joint_names_{"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
    std::vector<double> home_position_;  // Home position for startup
    
    // HEAD members
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr head_position_pub_;
    rclcpp::Subscription<std_msgs::msg::Int32>::SharedPtr head_position_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr head_ai_position_service_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr head_enable_service_;
    // Joint state publisher for robot_state_publisher (/joint_states)
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr joint_state_pub_;
    int latest_head_command_{0};
    std::mutex head_command_mutex_;
    std::atomic<bool> has_head_command_{false};
    
    // Health monitoring
    rclcpp::Publisher<maurice_msgs::msg::ArmStatus>::SharedPtr arm_status_pub_;
    rclcpp::TimerBase::SharedPtr health_timer_;
    maurice_msgs::msg::ArmStatus last_arm_status_;
    std::atomic<bool> arm_torque_enabled_{true};  // Track arm torque state

    // Control timer (replaces manual thread)
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
    struct GainProfile { int kp = 0, ki = 0, kd = 0; };
    static constexpr int kGainScheduleInterval = 20;  // control cycles between updates (~200ms at 100Hz)
    bool gs_enabled_ = false;
    std::array<GainProfile, 3> gs_near_;          // near profiles for joints 2, 3, 4
    std::array<GainProfile, 3> gs_far_;           // far profiles for joints 2, 3, 4
    std::array<GainProfile, 3> gs_last_applied_;  // last gains written (for change detection)
    int gs_cycle_counter_ = 0;
    bool gs_was_far_ = false;  // tracks last state for threshold crossing log

    // Control loop timing instrumentation
    long loop_us_sum_ = 0, loop_us_max_ = 0, loop_timing_count_ = 0;
    long gs_pid_write_us_sum_ = 0, gs_pid_write_count_ = 0;

    static constexpr int kLoadWarningThreshold = 800;  // ~80% load (0.1% units)
    static constexpr int kTemperatureWarningC = 70;
};

} // namespace maurice_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::MauriceArmNode>();
    
    // Motion planning via /mars/arm/goto_js is handled internally using MoveIt
    
    // Use multi-threaded executor with 4 threads to handle callbacks in parallel
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();
    
    rclcpp::shutdown();
    return 0;
}

