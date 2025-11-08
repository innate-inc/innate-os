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
#include <cmath>
#include <thread>
#include <chrono>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

namespace maurice_arm {

class MauriceArmNode : public rclcpp::Node {
public:
    MauriceArmNode() : Node("maurice_arm") {
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node starting...");
        
        // Declare parameters
        this->declare_parameter("baud_rate", 115200);
        this->declare_parameter("control_frequency", 100);
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
        std::vector<int> all_servo_ids = {1, 2, 3, 4, 5, 6, 7};
        robot_ = std::make_unique<Robot>(dynamixel_, all_servo_ids);
        
        // Setup ARM publishers/subscribers/services
        arm_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/maurice_arm/state", 10);
        arm_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
            "/maurice_arm/commands", 10,
            std::bind(&MauriceArmNode::armCommandCallback, this, std::placeholders::_1));
        
        arm_torque_on_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/maurice_arm/torque_on",
            std::bind(&MauriceArmNode::armTorqueOnCallback, this, std::placeholders::_1, std::placeholders::_2));
        
        arm_torque_off_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/maurice_arm/torque_off",
            std::bind(&MauriceArmNode::armTorqueOffCallback, this, std::placeholders::_1, std::placeholders::_2));
        
        // Setup HEAD publishers/subscribers/services
        head_position_pub_ = this->create_publisher<std_msgs::msg::String>("head/current_position", 10);
        head_position_sub_ = this->create_subscription<std_msgs::msg::Int32>(
            "head/set_position", 10,
            std::bind(&MauriceArmNode::headPositionCallback, this, std::placeholders::_1));
        
        head_ai_position_sub_ = this->create_subscription<std_msgs::msg::Empty>(
            "head/set_ai_position", 10,
            std::bind(&MauriceArmNode::headAiPositionCallback, this, std::placeholders::_1));
        
        head_enable_service_ = this->create_service<std_srvs::srv::SetBool>(
            "head/enable_servo",
            std::bind(&MauriceArmNode::headEnableServoCallback, this, std::placeholders::_1, std::placeholders::_2));
        
        // Initialize joint state message
        joint_state_msg_.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
        
        // Start unified control thread
        running_ = true;
        control_thread_ = std::thread(&MauriceArmNode::controlLoop, this);
        
        RCLCPP_INFO(this->get_logger(), "Maurice Arm Node ready!");
    }
    
    ~MauriceArmNode() {
        running_ = false;
        if (control_thread_.joinable()) control_thread_.join();
    }

private:
    void parseJointConfig(const std::string& json_str) {
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
                
                if (joint.contains("current_limit")) {
                    config.current_limit = joint["current_limit"];
                }
                
                if (joint.contains("homing_offset")) {
                    config.homing_offset = joint["homing_offset"];
                }
                
                config.kp = joint["pid_gains"]["kp"];
                config.ki = joint["pid_gains"]["ki"];
                config.kd = joint["pid_gains"]["kd"];
                
                // Parse head-specific config for joint 7
                if (i == 7 && joint.contains("head_config")) {
                    auto head = joint["head_config"];
                    config.head_min_angle_deg = head["min_angle_deg"];
                    config.head_max_angle_deg = head["max_angle_deg"];
                    config.head_ai_position_deg = head["ai_position_deg"];
                    config.head_direction_reversed = head["direction_reversed"];
                }
                
                joint_configs_.push_back(config);
            }
        }
    }
    
    void initializeServos() {
        RCLCPP_INFO(this->get_logger(), "Configuring all 7 servos...");
        
        // Configure all servos (IDs 1-7) uniformly
        for (const auto& config : joint_configs_) {
            RCLCPP_INFO(this->get_logger(), "Configuring servo %d", config.servo_id);
            
            dynamixel_->disableTorque(config.servo_id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set position limits
            int min_encoder = static_cast<int>((config.min_pos_rad / (2 * M_PI)) * 4096 + 2048);
            int max_encoder = static_cast<int>((config.max_pos_rad / (2 * M_PI)) * 4096 + 2048);
            dynamixel_->setMinPositionLimit(config.servo_id, min_encoder);
            dynamixel_->setMaxPositionLimit(config.servo_id, max_encoder);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set PWM limit
            dynamixel_->setPwmLimit(config.servo_id, config.pwm_limit);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set current limit if specified
            if (config.current_limit > 0) {
                dynamixel_->setCurrentLimit(config.servo_id, config.current_limit);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            
            // Set operating mode
            OperatingMode mode = OperatingMode::POSITION;
            if (config.control_mode == 1) mode = OperatingMode::VELOCITY;
            else if (config.control_mode == 3) mode = OperatingMode::POSITION;
            else if (config.control_mode == 5) mode = OperatingMode::CURRENT_CONTROLLED_POSITION;
            else if (config.control_mode == 16) mode = OperatingMode::PWM;
            
            dynamixel_->setOperatingMode(config.servo_id, mode);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            
            // Set homing offset if specified (for head servo)
            if (config.homing_offset != 0) {
                dynamixel_->setHomeOffset(config.servo_id, config.homing_offset);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            
            // Set PID gains
            dynamixel_->setP(config.servo_id, config.kp);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            dynamixel_->setI(config.servo_id, config.ki);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            dynamixel_->setD(config.servo_id, config.kd);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            
            // Enable torque
            dynamixel_->enableTorque(config.servo_id);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        // Move head to default position
        moveHeadToAngle(0.0);
    }
    
    // Unified control loop for both arm and head
    void controlLoop() {
        auto period = std::chrono::duration<double>(1.0 / control_frequency_);
        while (running_) {
            auto start = std::chrono::steady_clock::now();
            
            try {
                // ========== ARM CONTROL ==========
                // Read positions and velocities for arm servos
                auto positions = robot_->readPosition();
                auto velocities = robot_->readVelocity();
                
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
                
                // Publish arm joint state
                joint_state_msg_.header.stamp = this->now();
                joint_state_msg_.position = positions_rad;
                joint_state_msg_.velocity = velocities_rad;
                arm_state_pub_->publish(joint_state_msg_);
                
                // Process arm command if available
                {
                    std::lock_guard<std::mutex> lock(arm_command_mutex_);
                    if (has_arm_command_) {
                        robot_->setGoalPos(latest_arm_command_);
                        has_arm_command_ = false;
                    }
                }
                
                // ========== HEAD CONTROL ==========
                // Read head position (servo 7)
                int head_encoder = positions[6];  // Index 6 = servo 7
                
                // Publish head position
                publishHeadPosition(head_encoder);
                
                // Process head command if available
                if (has_head_command_) {
                    double angle;
                    {
                        std::lock_guard<std::mutex> lock(head_command_mutex_);
                        angle = head_command_angle_;
                        has_head_command_ = false;
                    }
                    
                    int head_goal_encoder = logicalAngleToEncoder(angle);
                    
                    // Update the goal position for servo 7
                    {
                        std::lock_guard<std::mutex> lock(arm_command_mutex_);
                        if (has_arm_command_ && latest_arm_command_.size() == 7) {
                            latest_arm_command_[6] = head_goal_encoder;
                        } else {
                            // Create command with current positions + new head position
                            latest_arm_command_ = positions;
                            latest_arm_command_[6] = head_goal_encoder;
                            has_arm_command_ = true;
                        }
                    }
                }
                
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Control loop error: %s", e.what());
            }
            
            auto end = std::chrono::steady_clock::now();
            auto elapsed = end - start;
            if (elapsed < period) {
                std::this_thread::sleep_for(period - elapsed);
            }
        }
    }
    
    void armCommandCallback(const std_msgs::msg::Float64MultiArray::SharedPtr msg) {
        try {
            std::vector<double> command_data(msg->data.begin(), msg->data.end());
            
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
        try {
            for (int id = 1; id <= 6; ++id) {
                dynamixel_->enableTorque(id);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            response->success = true;
            response->message = "Enabled torque for all arm servos";
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
        }
    }
    
    void armTorqueOffCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        try {
            for (int id = 1; id <= 6; ++id) {
                dynamixel_->disableTorque(id);
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            response->success = true;
            response->message = "Disabled torque for all arm servos";
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
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
            
            // Just update state variable - control loop will handle it
            std::lock_guard<std::mutex> lock(head_command_mutex_);
            head_command_angle_ = logical_position;
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
            
            // Just update state variable - control loop will handle it
            std::lock_guard<std::mutex> lock(head_command_mutex_);
            head_command_angle_ = head_config.head_ai_position_deg;
            has_head_command_ = true;
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in head AI position callback: %s", e.what());
        }
    }
    
    void headEnableServoCallback(
        const std::shared_ptr<std_srvs::srv::SetBool::Request> request,
        std::shared_ptr<std_srvs::srv::SetBool::Response> response) {
        try {
            if (request->data) {
                dynamixel_->enableTorque(7);
                response->message = "Head servo enabled";
            } else {
                dynamixel_->disableTorque(7);
                response->message = "Head servo disabled";
            }
            response->success = true;
        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed: ") + e.what();
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
    sensor_msgs::msg::JointState joint_state_msg_;
    std::vector<int> latest_arm_command_;
    std::mutex arm_command_mutex_;
    std::atomic<bool> has_arm_command_{false};
    
    // HEAD members
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr head_position_pub_;
    rclcpp::Subscription<std_msgs::msg::Int32>::SharedPtr head_position_sub_;
    rclcpp::Subscription<std_msgs::msg::Empty>::SharedPtr head_ai_position_sub_;
    rclcpp::Service<std_srvs::srv::SetBool>::SharedPtr head_enable_service_;
    double head_command_angle_{0.0};
    std::mutex head_command_mutex_;
    std::atomic<bool> has_head_command_{false};
    
    // Unified control thread
    std::thread control_thread_;
    std::atomic<bool> running_{false};
    double control_frequency_;
};

} // namespace maurice_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::MauriceArmNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}

