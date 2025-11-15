#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/int32.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_msgs/msg/empty.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <std_srvs/srv/set_bool.hpp>
#include <visualization_msgs/msg/marker.hpp>
#include <visualization_msgs/msg/marker_array.hpp>
#include "maurice_arm/dynamixel.hpp"
#include "maurice_arm/robot.hpp"
#include "maurice_arm/collision_checker.hpp"
#include <cmath>
#include <chrono>
#include <nlohmann/json.hpp>

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
        this->declare_parameter("publish_collision_markers", true);
        
        int baud_rate = this->get_parameter("baud_rate").as_int();
        control_frequency_ = this->get_parameter("control_frequency").as_double();
        std::string joints_str = this->get_parameter("joints").as_string();
        publish_collision_markers_ = this->get_parameter("publish_collision_markers").as_bool();
        
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
        
        if (publish_collision_markers_) {
            collision_marker_pub_ = this->create_publisher<visualization_msgs::msg::MarkerArray>(
                "/mars/arm/collision_shapes", 10);
            RCLCPP_INFO(this->get_logger(), "Collision shape markers enabled on /mars/arm/collision_shapes");
        }
        
        arm_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
            "/mars/arm/commands", 10,
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
        
        // Setup HEAD publishers/subscribers/services
        RCLCPP_INFO(this->get_logger(), "Setting up HEAD publishers/subscribers/services");
        head_position_pub_ = this->create_publisher<std_msgs::msg::String>("/mars/head/current_position", 10);
        head_position_sub_ = this->create_subscription<std_msgs::msg::Int32>(
            "/mars/head/set_position", 10,
            std::bind(&MauriceArmNode::headPositionCallback, this, std::placeholders::_1));
        
        head_ai_position_sub_ = this->create_subscription<std_msgs::msg::Empty>(
            "/mars/head/set_ai_position", 10,
            std::bind(&MauriceArmNode::headAiPositionCallback, this, std::placeholders::_1));
        
        head_enable_service_ = this->create_service<std_srvs::srv::SetBool>(
            "/mars/head/enable_servo",
            std::bind(&MauriceArmNode::headEnableServoCallback, this, std::placeholders::_1, std::placeholders::_2),
            rmw_qos_profile_services_default,
            service_callback_group_);
        
        // Initialize joint state message
        RCLCPP_INFO(this->get_logger(), "Initializing joint state message with 6 joint names");
        joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
        
        // Initialize command buffers with current positions
        RCLCPP_INFO(this->get_logger(), "Initializing command buffers with current positions");
        auto [initial_positions, initial_velocities] = robot_->readState();
        latest_arm_command_ = std::vector<int>(initial_positions.begin(), initial_positions.begin() + 6);
        latest_head_command_ = initial_positions[6];
        RCLCPP_INFO(this->get_logger(), "Command buffers initialized (arm: 6 joints, head: 1 joint)");
        
        // Initialize collision checker
        RCLCPP_INFO(this->get_logger(), "Initializing collision checker...");
        collision_checker_ = std::make_unique<CollisionCheckerCore>();
        
        // Initialize last_safe_position_ with current position (EXTERNAL convention)
        last_safe_position_.resize(6);
        for (size_t i = 0; i < 6; ++i) {
            last_safe_position_[i] = ((initial_positions[i] - 2048) * 2 * M_PI) / 4096.0;
        }
        // Apply direction flips to convert to EXTERNAL convention (same as published joint states)
        std::array<size_t, 4> flip_indices = {1, 2, 3, 5};
        for (size_t idx : flip_indices) {
            last_safe_position_[idx] = -last_safe_position_[idx];
        }
        RCLCPP_INFO(this->get_logger(), "Collision checker initialized with current position as safe (external convention)");
        
        // Create timer for control loop
        RCLCPP_INFO(this->get_logger(), "Creating control timer at %.1f Hz", control_frequency_);
        auto period = std::chrono::duration<double>(1.0 / control_frequency_);
        control_timer_ = this->create_wall_timer(
            std::chrono::duration_cast<std::chrono::nanoseconds>(period),
            std::bind(&MauriceArmNode::controlTimerCallback, this),
            timer_callback_group_);
        
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node ready!");
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
                if (i == 7 && joint.contains("head_config")) {
                    auto head = joint["head_config"];
                    config.head_min_angle_deg = head["min_angle_deg"];
                    config.head_max_angle_deg = head["max_angle_deg"];
                    config.head_ai_position_deg = head["ai_position_deg"];
                    config.head_direction_reversed = head["direction_reversed"];
                    RCLCPP_INFO(this->get_logger(), "  Head config: range=[%.1f, %.1f] deg, AI pos=%.1f deg, reversed=%s",
                        config.head_min_angle_deg, config.head_max_angle_deg, config.head_ai_position_deg,
                        config.head_direction_reversed ? "true" : "false");
                }
                
                joint_configs_.push_back(config);
            }
        }
        RCLCPP_INFO(this->get_logger(), "Parsed %zu joint configurations", joint_configs_.size());
    }
    
    void initializeServos() {
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
        moveHeadToAngle(0.0);
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
            
            // ========== PUBLISH COLLISION MARKERS ==========
            if (publish_collision_markers_) {
                std::vector<double> current_joint_pos(positions_rad.begin(), positions_rad.begin() + 6);
                publishCollisionMarkers(current_joint_pos);
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
    
    // Distance-based command scaling for collision avoidance with per-link and per-joint scaling
    // NOTE: commanded_pos should be in EXTERNAL convention (same as published joint states)
    std::vector<double> scaleCommandByClearance(const std::vector<double>& commanded_pos) {
        auto check_start = std::chrono::high_resolution_clock::now();
        
        // Check collision at commanded position (external convention)
        auto check_result = collision_checker_->checkConfiguration(commanded_pos);
        
        auto check_end = std::chrono::high_resolution_clock::now();
        auto check_duration = std::chrono::duration_cast<std::chrono::microseconds>(check_end - check_start);
        
        RCLCPP_DEBUG_THROTTLE(this->get_logger(), *this->get_clock(), 200,
            "Collision check: %ld μs, clearance: %.1f mm, %s",
            check_duration.count(),
            check_result.min_clearance * 1000.0,
            check_result.closest_pair.c_str());
        
        // Compute per-joint scale factors based on link clearances
        std::vector<double> joint_scales(6, 1.0);  // Start with full motion for all joints
        bool any_restriction = false;
        
        // Debug: log all link clearances when there's a collision
        if (check_result.in_collision || check_result.min_clearance < 0.010) {
            RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                "Link clearances: link2=%.1fmm link3=%.1fmm link61=%.1fmm link62=%.1fmm",
                check_result.link_clearances["link2"] * 1000.0,
                check_result.link_clearances["link3"] * 1000.0,
                check_result.link_clearances["link61"] * 1000.0,
                check_result.link_clearances["link62"] * 1000.0);
        }
        
        for (const auto& [link_name, clearance] : check_result.link_clearances) {
            // Skip base_link - it's fixed and shouldn't restrict motion
            if (link_name == "base_link") {
                continue;
            }
            
            // Get link-specific margins
            double safety_margin = link_safety_margins_[link_name];
            double warning_distance = link_warning_distances_[link_name];
            
            double link_scale = 1.0;
            
            // Handle collision (negative or very small clearance)
            if (clearance <= 0.0 || (check_result.in_collision && clearance < safety_margin)) {
                // In collision - freeze all joints affecting this link
                link_scale = 0.0;
                any_restriction = true;
                
                // Only warn if this link has joints to freeze
                if (!link_to_joints_.at(link_name).empty()) {
                    auto closest_it = check_result.link_closest_to.find(link_name);
                    std::string closest = (closest_it != check_result.link_closest_to.end()) 
                                          ? closest_it->second : "unknown";
                    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                        "%s IN COLLISION with %s, freezing dependent joints",
                        link_name.c_str(), closest.c_str());
                }
            }
            // Handle too close (within safety margin)
            else if (clearance < safety_margin) {
                // Too close - stop all joints affecting this link
                link_scale = 0.0;
                any_restriction = true;
                
                if (!link_to_joints_.at(link_name).empty()) {
                    auto closest_it = check_result.link_closest_to.find(link_name);
                    std::string closest = (closest_it != check_result.link_closest_to.end()) 
                                          ? closest_it->second : "unknown";
                    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                        "%s too close (%.1f mm < %.1f mm) to %s, freezing dependent joints",
                        link_name.c_str(), clearance * 1000.0, safety_margin * 1000.0,
                        closest.c_str());
                }
            }
            // Handle warning zone (between safety margin and warning distance)
            else if (clearance < warning_distance) {
                // In warning zone - scale between 0 and 1 based on clearance
                link_scale = (clearance - safety_margin) / (warning_distance - safety_margin);
                link_scale = std::clamp(link_scale, 0.0, 1.0);
                any_restriction = true;
                
                RCLCPP_DEBUG_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                    "%s in warning zone (%.1f mm), scaling to %.0f%%",
                    link_name.c_str(), clearance * 1000.0, link_scale * 100.0);
            }
            
            // Apply this scale to all joints that affect this link (take minimum scale)
            if (link_scale < 1.0) {
                std::string frozen_joints = "";
                for (size_t joint_idx : link_to_joints_[link_name]) {
                    joint_scales[joint_idx] = std::min(joint_scales[joint_idx], link_scale);
                    frozen_joints += "J" + std::to_string(joint_idx + 1) + " ";
                }
                if (!frozen_joints.empty() && link_scale == 0.0) {
                    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                        "  -> Freezing joints %sdue to %s (clearance: %.1fmm)", 
                        frozen_joints.c_str(), link_name.c_str(), clearance * 1000.0);
                }
            }
        }
        
        // If no restrictions, accept command as-is
        if (!any_restriction) {
            last_safe_position_ = commanded_pos;
            return commanded_pos;
        }
        
        // Apply per-joint scaling
        std::vector<double> scaled_pos(6);
        for (size_t i = 0; i < 6; ++i) {
            scaled_pos[i] = last_safe_position_[i] + 
                           joint_scales[i] * (commanded_pos[i] - last_safe_position_[i]);
        }
        
        // Log joint scales when there's any restriction
        if (any_restriction) {
            RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                "Joint scales: J1=%.0f%% J2=%.0f%% J3=%.0f%% J4=%.0f%% J5=%.0f%% J6=%.0f%%",
                joint_scales[0] * 100.0, joint_scales[1] * 100.0, joint_scales[2] * 100.0,
                joint_scales[3] * 100.0, joint_scales[4] * 100.0, joint_scales[5] * 100.0);
        }
        
        // Update last safe position for joints that were allowed to move
        for (size_t i = 0; i < 6; ++i) {
            if (joint_scales[i] > 0.0) {
                last_safe_position_[i] = scaled_pos[i];
            }
            // If joint_scales[i] == 0.0, keep the last safe position for that joint
        }
        
        return scaled_pos;
    }
    
    void armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg) {
        auto callback_start = std::chrono::high_resolution_clock::now();
        
        try {
            // Commands come in EXTERNAL convention (same as published joint states)
            std::vector<double> command_data(msg->data.begin(), msg->data.end());
            
            // Validate that we have exactly 6 arm joints
            if (command_data.size() != 6) {
                RCLCPP_ERROR(this->get_logger(), "Action size must match number of servos. Expected 6, got %zu", command_data.size());
                return;
            }
            
            // ===== COLLISION-BASED COMMAND FILTERING =====
            // Pass EXTERNAL convention to collision checker (same as visualization)
            std::vector<double> safe_command = scaleCommandByClearance(command_data);
            
            // Now convert from EXTERNAL to HARDWARE convention for servos
            // Apply direction flips for joints 2, 3, 4, 6 (indices 1, 2, 3, 5)
            std::array<size_t, 4> flip_indices = {1, 2, 3, 5};
            for (size_t idx : flip_indices) {
                if (idx < safe_command.size()) {
                    safe_command[idx] = -safe_command[idx];
                }
            }
            
            // Convert to encoder counts (only 6 arm joints)
            // Using safe_command (collision-filtered, hardware convention)
            std::vector<int> command_encoder;
            for (double pos : safe_command) {
                command_encoder.push_back(static_cast<int>((pos / (2 * M_PI)) * 4096 + 2048));
            }
            
            // Store only the 6 arm positions - timer will add head position
            std::lock_guard<std::mutex> lock(arm_command_mutex_);
            latest_arm_command_ = command_encoder;
            has_arm_command_ = true;
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in arm command callback: %s", e.what());
        }
        
        auto callback_end = std::chrono::high_resolution_clock::now();
        auto total_duration = std::chrono::duration_cast<std::chrono::microseconds>(callback_end - callback_start);
        
        RCLCPP_DEBUG_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
            "Command callback total: %ld μs", total_duration.count());
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
        int encoder_value = logicalAngleToEncoder(logical_angle_deg);
        dynamixel_->setGoalPosition(7, encoder_value);
    }
    
    void publishHeadPosition(int encoder_value) {
        double logical_angle = encoderToLogicalAngle(encoder_value);
        
        // Get head config for limits
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
            
            // Get head config for limits
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
    
    void headAiPositionCallback(const std_msgs::msg::Empty::SharedPtr /*msg*/) {
        try {
            // Get head config for AI position
            const auto& head_config = joint_configs_[6];  // Index 6 = joint 7
            
            RCLCPP_INFO(this->get_logger(), "Moving head to AI position (%f deg)", 
                head_config.head_ai_position_deg);
            
            int head_goal_encoder = logicalAngleToEncoder(head_config.head_ai_position_deg);
            
            std::lock_guard<std::mutex> lock(head_command_mutex_);
            latest_head_command_ = head_goal_encoder;
            has_head_command_ = true;
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in head AI position callback: %s", e.what());
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
    
    // Publish collision shape visualization markers
    // NOTE: joint_positions should be in EXTERNAL convention (same as published joint states)
    void publishCollisionMarkers(const std::vector<double>& joint_positions) {
        auto marker_array = visualization_msgs::msg::MarkerArray();
        int id = 0;
        
        // Compute forward kinematics (external convention)
        auto transforms = collision_checker_->computeForwardKinematics(joint_positions);
        
        // Get collision geometries from collision checker
        const auto& collision_geometries = collision_checker_->getCollisionGeometries();
        
        // Check current collision status
        auto check_result = collision_checker_->checkConfiguration(joint_positions);
        bool collision_detected = check_result.in_collision;
        
        // Publish collision shape markers
        for (const auto& [link_name, geom] : collision_geometries) {
            if (transforms.find(link_name) == transforms.end()) continue;
            
            auto marker = visualization_msgs::msg::Marker();
            marker.header.frame_id = "base_link";
            marker.header.stamp = this->now();
            marker.ns = "collision_shapes";
            marker.id = id++;
            marker.action = visualization_msgs::msg::Marker::ADD;
            
            // Get transform
            Eigen::Isometry3d link_transform = transforms.at(link_name);
            Eigen::Isometry3d offset_transform = collision_checker_->createTransform(geom.offset, geom.rpy);
            Eigen::Isometry3d final_transform = link_transform * offset_transform;
            
            // Set pose
            marker.pose.position.x = final_transform.translation().x();
            marker.pose.position.y = final_transform.translation().y();
            marker.pose.position.z = final_transform.translation().z();
            
            Eigen::Quaterniond q(final_transform.rotation());
            marker.pose.orientation.x = q.x();
            marker.pose.orientation.y = q.y();
            marker.pose.orientation.z = q.z();
            marker.pose.orientation.w = q.w();
            
            // Set shape
            if (geom.type == CollisionGeometry::Type::BOX) {
                marker.type = visualization_msgs::msg::Marker::CUBE;
                auto box = std::static_pointer_cast<fcl::Boxd>(geom.shape);
                marker.scale.x = box->side[0];
                marker.scale.y = box->side[1];
                marker.scale.z = box->side[2];
            } else if (geom.type == CollisionGeometry::Type::CYLINDER) {
                marker.type = visualization_msgs::msg::Marker::CYLINDER;
                auto cyl = std::static_pointer_cast<fcl::Cylinderd>(geom.shape);
                marker.scale.x = cyl->radius * 2;
                marker.scale.y = cyl->radius * 2;
                marker.scale.z = cyl->lz;
            }
            
            // Set color (green if no collision, red if collision)
            marker.color.r = collision_detected ? 1.0 : 0.0;
            marker.color.g = collision_detected ? 0.0 : 1.0;
            marker.color.b = 0.0;
            marker.color.a = 0.5;
            
            marker_array.markers.push_back(marker);
        }
        
        // Publish ground plane marker
        auto ground_marker = visualization_msgs::msg::Marker();
        ground_marker.header.frame_id = "base_link";
        ground_marker.header.stamp = this->now();
        ground_marker.ns = "collision_shapes";
        ground_marker.id = id++;
        ground_marker.action = visualization_msgs::msg::Marker::ADD;
        ground_marker.type = visualization_msgs::msg::Marker::CUBE;
        
        ground_marker.pose.position.x = 0.0;
        ground_marker.pose.position.y = 0.0;
        ground_marker.pose.position.z = -0.05;  // Show thin surface layer at ground level
        ground_marker.pose.orientation.w = 1.0;
        
        ground_marker.scale.x = 10.0;  // 10m x 10m for visualization (actual collision plane is 100m x 100m)
        ground_marker.scale.y = 10.0;
        ground_marker.scale.z = 0.1;  // Thin visualization layer (actual collision extends 10m deep)
        
        ground_marker.color.r = 0.5;
        ground_marker.color.g = 0.5;
        ground_marker.color.b = 0.5;
        ground_marker.color.a = 0.3;
        
        marker_array.markers.push_back(ground_marker);
        
        collision_marker_pub_->publish(marker_array);
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
        double head_min_angle_deg = 0.0;
        double head_max_angle_deg = 0.0;
        double head_ai_position_deg = 0.0;
        bool head_direction_reversed = false;
    };
    
    std::shared_ptr<Dynamixel> dynamixel_;
    std::unique_ptr<Robot> robot_;
    std::vector<JointConfig> joint_configs_;
    
    // ARM members
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr arm_state_pub_;
    rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr collision_marker_pub_;
    rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr arm_command_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_on_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_off_service_;
    sensor_msgs::msg::JointState joint_state_msg_;
    std::vector<int> latest_arm_command_;
    std::mutex arm_command_mutex_;
    std::atomic<bool> has_arm_command_{false};
    bool publish_collision_markers_;
    
    // HEAD members
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr head_position_pub_;
    rclcpp::Subscription<std_msgs::msg::Int32>::SharedPtr head_position_sub_;
    rclcpp::Subscription<std_msgs::msg::Empty>::SharedPtr head_ai_position_sub_;
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
    
    // Collision checker for safe command filtering
    std::unique_ptr<CollisionCheckerCore> collision_checker_;
    std::vector<double> last_safe_position_;  // Last known safe position (6 joints, in radians)
    
    // Link-to-joint dependency mapping: which joints affect which links
    // Links: base_link, link2, link3, link61, link62
    // Joints: 0=joint1, 1=joint2, 2=joint3, 3=joint4, 4=joint5, 5=joint6
    std::map<std::string, std::vector<size_t>> link_to_joints_ = {
        {"base_link", {}},                    // Fixed base, no joints affect it
        {"link2", {0, 1}},                    // Affected by joint1, joint2
        {"link3", {0, 1, 2}},                 // Affected by joint1, joint2, joint3
        {"link61", {0, 1, 2, 3, 4, 5}},      // Affected by all joints (gripper finger)
        {"link62", {0, 1, 2, 3, 4, 5}}       // Affected by all joints (gripper finger)
    };
    
    // Link-specific safety margins (hard stop distance)
    std::map<std::string, double> link_safety_margins_ = {
        {"base_link", 0.002},    // 2mm for base (less critical, mostly fixed)
        {"link2", 0.002},        // 2mm for proximal link
        {"link3", 0.003},        // 3mm for main arm link (moves faster)
        {"link61", 0.001},       // 1mm for gripper finger (needs precision)
        {"link62", 0.001}        // 1mm for gripper finger (needs precision)
    };
    
    // Link-specific warning distances (start scaling motion)
    std::map<std::string, double> link_warning_distances_ = {
        {"base_link", 0.010},    // 10mm warning zone for base
        {"link2", 0.010},        // 10mm warning zone
        {"link3", 0.015},        // 15mm warning zone (larger due to speed)
        {"link61", 0.008},       // 8mm warning zone for gripper
        {"link62", 0.008}        // 8mm warning zone for gripper
    };
};

} // namespace maurice_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::MauriceArmNode>();
    
    // Use multi-threaded executor with 4 threads to handle callbacks in parallel
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();
    
    rclcpp::shutdown();
    return 0;
}

