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

using json = nlohmann::json;

namespace maurice_arm {

class MauriceArmNode : public rclcpp::Node {
public:
    MauriceArmNode() : Node("maurice_arm") {
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node starting...");
        
        // Create callback groups for parallel execution
        timer_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
        service_callback_group_ = this->create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
        
        // Declare parameters
        this->declare_parameter("baud_rate", 1000000);
        this->declare_parameter("control_frequency", 100.0);
        this->declare_parameter("joints", "{}");
        
        int baud_rate = this->get_parameter("baud_rate").as_int();
        control_frequency_ = this->get_parameter("control_frequency").as_double();
        std::string joints_str = this->get_parameter("joints").as_string();
        
        // Parse joints configuration
        parseJointConfig(joints_str);
        
        // Use fixed device path
        std::string device_name = "/dev/ttyTHS1";
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
        
        // Initialize joint state message
        RCLCPP_INFO(this->get_logger(), "Initializing joint state message with 6 joint names");
        joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
        
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
    
    void initializeServos() {
        std::lock_guard<std::mutex> lock(dynamixel_mutex_);
        configureServosLocked();
    }

    void configureServosLocked() {
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
            
            // Set PID gains
            RCLCPP_INFO(this->get_logger(), "  Setting PID gains: P=%d, I=%d, D=%d", config.kp, config.ki, config.kd);
            dynamixel_->setP(config.servo_id, config.kp);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            dynamixel_->setI(config.servo_id, config.ki);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            dynamixel_->setD(config.servo_id, config.kd);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            
            // Enable torque
            RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", config.servo_id);
            dynamixel_->enableTorque(config.servo_id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        // Move head to default position
        RCLCPP_INFO(this->get_logger(), "Moving head to default position (0.0 deg)");
        moveHeadToAngleLocked(0.0);
    }
    
    // Timer callback for unified control loop (replaces thread-based loop)
    void controlTimerCallback() {
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            // ========== READ STATE ==========
            auto [positions, velocities] = robot_->readState();
            
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
            
            // Publish arm joint state (only first 6 servos)
            joint_state_msg_.header.stamp = this->now();
            joint_state_msg_.position = std::vector<double>(positions_rad.begin(), positions_rad.begin() + 6);
            joint_state_msg_.velocity = std::vector<double>(velocities_rad.begin(), velocities_rad.begin() + 6);
            arm_state_pub_->publish(joint_state_msg_);
            
            // Store latest joint positions for trajectory planning
            {
                std::lock_guard<std::mutex> js_lock(joint_state_mutex_);
                latest_joint_positions_ = joint_state_msg_.position;
            }
            
            // ========== PUBLISH HEAD POSITION ==========
            int head_encoder = positions[6];  // Index 6 = servo 7
            publishHeadPosition(head_encoder);
            
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
            
            // Convert to encoder counts (only 6 arm joints)
            std::vector<int> command_encoder;
            for (double pos : command_data) {
                command_encoder.push_back(static_cast<int>((pos / (2 * M_PI)) * 4096 + 2048));
            }
            
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
            
            RCLCPP_INFO(this->get_logger(), "Reapplying configuration after reboot");
            configureServosLocked();
            
            response->success = true;
            response->message = "Rebooted and reinitialized all servos";
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
     * Internal function to plan and execute a trajectory to a target joint state.
     * This can be called from the service callback or any other part of the code.
     * 
     * @param target_positions Target joint positions in radians (6 joints including gripper)
     * @param trajectory_time Total time for trajectory execution in seconds (not used for MoveIt)
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
        
        // Plan with MoveIt (6 DOF arm including gripper)
        RCLCPP_INFO(this->get_logger(), "Planning with MoveIt for 6-DOF arm (including gripper)...");
        auto [waypoints, time_from_start] = planWithMoveIt(current_positions, target_positions, trajectory_time);
        
        if (waypoints.empty()) {
            RCLCPP_ERROR(this->get_logger(), "MoveIt planning failed");
            return false;
        }
        
        // Check if MoveIt provided timing info, if not, create our own
        bool has_timing = !time_from_start.empty() && time_from_start.back() > 0.0;
        
        if (!has_timing) {
            RCLCPP_WARN(this->get_logger(), "MoveIt didn't time-parameterize trajectory, doing it manually");
            // Distribute waypoints evenly over the requested trajectory_time
            time_from_start.clear();
            for (size_t i = 0; i < waypoints.size(); ++i) {
                double t = (static_cast<double>(i) / (waypoints.size() - 1)) * trajectory_time;
                time_from_start.push_back(t);
            }
        }
        
        // Interpolate trajectory to 30 Hz
        const double dt = 1.0 / 30.0;
        auto interpolated_trajectory = interpolateMoveItTrajectory(waypoints, time_from_start, dt);
        
        if (interpolated_trajectory.empty()) {
            RCLCPP_ERROR(this->get_logger(), "Trajectory interpolation failed");
            return false;
        }
        
        RCLCPP_INFO(this->get_logger(), "Executing trajectory with %zu waypoints over %.2f seconds", 
                    interpolated_trajectory.size(), trajectory_time);
        
        // Execute trajectory by sending each waypoint with a sleep
        auto sleep_duration = std::chrono::duration<double>(dt);
        for (size_t i = 0; i < interpolated_trajectory.size(); ++i) {
            const auto& point = interpolated_trajectory[i];
            
            // Send command
            {
                std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
                
                // Convert radians to encoder counts
                std::vector<double> command_data = point;
                
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
                
                latest_arm_command_ = command_encoder;
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
    sensor_msgs::msg::JointState joint_state_msg_;
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
    int latest_head_command_{0};
    std::mutex head_command_mutex_;
    std::atomic<bool> has_head_command_{false};
    
    // Control timer (replaces manual thread)
    rclcpp::TimerBase::SharedPtr control_timer_;
    double control_frequency_;
    
    // Callback groups for parallel execution
    rclcpp::CallbackGroup::SharedPtr timer_callback_group_;
    rclcpp::CallbackGroup::SharedPtr service_callback_group_;
    
    // Mutex to protect Dynamixel serial bus access
    std::mutex dynamixel_mutex_;
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

