#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/int32.hpp>
#include <std_msgs/msg/string.hpp>
#include <std_msgs/msg/empty.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <std_srvs/srv/set_bool.hpp>
#include <tf2_ros/transform_broadcaster.h>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include "maurice_arm/dynamixel.hpp"
#include "maurice_arm/robot.hpp"
#include <cmath>
#include <chrono>
#include <nlohmann/json.hpp>
#include <thread>

using json = nlohmann::json;

namespace maurice_arm {

// ============================================================================
// Camera Geometry Constants (from calibration)
// ============================================================================
// Camera horizontal FOV in degrees (from calibration_data/camera_calibration_results.json)
constexpr double CAMERA_HFOV_DEG = 105.26455076752997;

// Base offset from head rotation axis to camera (when head is level) in meters
// Measured from CAD: [-40.75, 0, 258.082] mm
constexpr double CAM_BASE_OFFSET_X = -40.75 / 1000.0;
constexpr double CAM_BASE_OFFSET_Y = 0.0;
constexpr double CAM_BASE_OFFSET_Z = 258.082 / 1000.0;

// Focal offset component: 9.41 / (2 * tan(hfov/2)) in meters
// This accounts for the lens offset from the sensor plane
inline double computeFocalOffset() {
    return 9.41 / (2.0 * std::tan(CAMERA_HFOV_DEG * M_PI / 360.0)) / 1000.0;
}

// Rotating offset (before applying cos(theta)) in meters
// These components scale with cos(head_pitch)
constexpr double CAM_ROTATING_BASE_X = 40.27 / 1000.0;  // Will add focal offset
constexpr double CAM_ROTATING_OFFSET_Y = -30.0 / 1000.0;
constexpr double CAM_ROTATING_OFFSET_Z = 0.0;

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
        
        arm_reboot_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/arm/reboot",
            std::bind(&MauriceArmNode::armRebootServosCallback, this, std::placeholders::_1, std::placeholders::_2),
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
        
        // Setup TF broadcaster for head camera transform
        RCLCPP_INFO(this->get_logger(), "Setting up TF broadcaster for head camera");
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);
        focal_offset_ = computeFocalOffset();
        RCLCPP_INFO(this->get_logger(), "Camera focal offset: %.4f m", focal_offset_);
        
        // Initialize joint state message
        RCLCPP_INFO(this->get_logger(), "Initializing joint state message with 6 joint names");
        joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
        
        // Initialize command buffers with current positions
        RCLCPP_INFO(this->get_logger(), "Initializing command buffers with current positions");
        auto [initial_positions, initial_velocities] = robot_->readState();
        latest_arm_command_ = std::vector<int>(initial_positions.begin(), initial_positions.begin() + 6);
        latest_head_command_ = initial_positions[6];
        RCLCPP_INFO(this->get_logger(), "Command buffers initialized (arm: 6 joints, head: 1 joint)");
        
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
            std::vector<double> command_data(msg->data.begin(), msg->data.end());
            
            // Validate that we have exactly 6 arm joints
            if (command_data.size() != 6) {
                RCLCPP_ERROR(this->get_logger(), "Action size must match number of servos. Expected 6, got %zu", command_data.size());
                return;
            }
            
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
        
        // Also publish the camera transform
        publishHeadCameraTransform(logical_angle);
    }
    
    /**
     * @brief Publish TF transform from base_link to head camera optical frame
     * 
     * Camera geometry:
     *   - Base offset: fixed offset from head rotation axis to camera (at pitch=0)
     *   - Rotating offset: rotated about Y axis by pitch angle
     *   - For rotation about Y: x' = x*cos(θ), y' = y, z' = -x*sin(θ)
     * 
     * Frame conventions:
     *   - head_camera_link: camera mount frame (X forward, Y left, Z up)
     *   - head_camera_optical_frame: optical frame (Z forward, X right, Y down)
     * 
     * @param pitch_deg Head pitch angle in degrees (negative = looking down)
     */
    void publishHeadCameraTransform(double pitch_deg) {
        // Convert pitch to radians
        // Note: pitch_deg convention is negative = looking down
        // For rotation about Y axis, we use the angle directly
        double pitch_rad = pitch_deg * M_PI / 180.0;
        double cos_pitch = std::cos(pitch_rad);
        double sin_pitch = std::sin(pitch_rad);
        
        // Compute rotating offset X component (includes focal offset)
        double rotating_offset_x = CAM_ROTATING_BASE_X + focal_offset_;
        
        // Rotate the offset about Y axis (pitch axis)
        // Convention: pitch_deg negative = looking down, positive = looking up
        // Geometric rotation θ = -pitch_deg, so:
        //   z' = -x*sin(θ) = -x*sin(-pitch_deg) = x*sin(pitch_deg)
        double rotated_x = rotating_offset_x * cos_pitch;
        double rotated_y = CAM_ROTATING_OFFSET_Y;  // Y is constant (rotation axis)
        double rotated_z = rotating_offset_x * sin_pitch;  // Positive pitch → Z up
        
        // Total camera position = base_offset + rotated_offset
        double cam_x = CAM_BASE_OFFSET_X + rotated_x;
        double cam_y = CAM_BASE_OFFSET_Y + rotated_y;
        double cam_z = CAM_BASE_OFFSET_Z + rotated_z;
        
        // Create and publish transform for head_camera_link (camera mount frame)
        geometry_msgs::msg::TransformStamped transform_msg;
        transform_msg.header.stamp = this->now();
        transform_msg.header.frame_id = "base_link";
        transform_msg.child_frame_id = "head_camera_link";
        
        // Translation
        transform_msg.transform.translation.x = cam_x;
        transform_msg.transform.translation.y = cam_y;
        transform_msg.transform.translation.z = cam_z;
        
        // Rotation: camera tilts with head pitch (rotation about Y axis)
        // Using quaternion for rotation about Y axis: q = [0, sin(θ/2), 0, cos(θ/2)]
        // Note: pitch_deg is the logical angle, negative means looking down
        // Camera optical axis tilts down when pitch is negative
        double half_pitch = pitch_rad / 2.0;
        transform_msg.transform.rotation.x = 0.0;
        transform_msg.transform.rotation.y = std::sin(half_pitch);
        transform_msg.transform.rotation.z = 0.0;
        transform_msg.transform.rotation.w = std::cos(half_pitch);
        
        tf_broadcaster_->sendTransform(transform_msg);
        
        // Also publish the optical frame (camera_optical_frame convention: Z forward, X right, Y down)
        // This is a rotation of -90° about X, then -90° about Z from camera_link
        // Or equivalently: X_opt = Y_link, Y_opt = -Z_link, Z_opt = X_link
        // Quaternion for this rotation: combine pitch rotation with optical frame rotation
        geometry_msgs::msg::TransformStamped optical_transform_msg;
        optical_transform_msg.header.stamp = this->now();
        optical_transform_msg.header.frame_id = "base_link";
        optical_transform_msg.child_frame_id = "head_camera_optical_frame";
        
        // Same translation as camera_link
        optical_transform_msg.transform.translation.x = cam_x;
        optical_transform_msg.transform.translation.y = cam_y;
        optical_transform_msg.transform.translation.z = cam_z;
        
        // Combined rotation: pitch about Y, then optical frame transform
        // Optical frame rotation (from ROS camera_link to optical): 
        //   Rz(-90°) * Rx(-90°) or equivalently Ry(-90°) * Rx(-90°)
        // q_optical = [-0.5, 0.5, -0.5, 0.5] (for standard optical frame transform)
        // Combined: q_combined = q_pitch * q_optical
        double q_opt_x = -0.5;
        double q_opt_y = 0.5;
        double q_opt_z = -0.5;
        double q_opt_w = 0.5;
        
        // Quaternion multiplication: q_pitch * q_optical
        double q_pitch_y = std::sin(half_pitch);
        double q_pitch_w = std::cos(half_pitch);
        
        // q1 = [0, q_pitch_y, 0, q_pitch_w]
        // q2 = [q_opt_x, q_opt_y, q_opt_z, q_opt_w]
        // q1 * q2:
        double qx = q_pitch_w * q_opt_x + q_pitch_y * q_opt_z;
        double qy = q_pitch_w * q_opt_y + q_pitch_y * q_opt_w;
        double qz = q_pitch_w * q_opt_z - q_pitch_y * q_opt_x;
        double qw = q_pitch_w * q_opt_w - q_pitch_y * q_opt_y;
        
        optical_transform_msg.transform.rotation.x = qx;
        optical_transform_msg.transform.rotation.y = qy;
        optical_transform_msg.transform.rotation.z = qz;
        optical_transform_msg.transform.rotation.w = qw;
        
        tf_broadcaster_->sendTransform(optical_transform_msg);
        
        // Also publish "camera" frame for compatibility with maurice_arm/camera.cpp
        // which uses frame_id="camera" for published images
        geometry_msgs::msg::TransformStamped camera_transform_msg = transform_msg;
        camera_transform_msg.child_frame_id = "camera";
        tf_broadcaster_->sendTransform(camera_transform_msg);
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
    rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr arm_command_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_on_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_off_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_reboot_service_;
    sensor_msgs::msg::JointState joint_state_msg_;
    std::vector<int> latest_arm_command_;
    std::mutex arm_command_mutex_;
    std::atomic<bool> has_arm_command_{false};
    
    // HEAD members
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr head_position_pub_;
    rclcpp::Subscription<std_msgs::msg::Int32>::SharedPtr head_position_sub_;
    rclcpp::Subscription<std_msgs::msg::Empty>::SharedPtr head_ai_position_sub_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr head_enable_service_;
    int latest_head_command_{0};
    std::mutex head_command_mutex_;
    std::atomic<bool> has_head_command_{false};
    
    // TF broadcaster for head camera transform
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    double focal_offset_;  // Computed once at startup
    
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
    
    // Use multi-threaded executor with 4 threads to handle callbacks in parallel
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();
    
    rclcpp::shutdown();
    return 0;
}

