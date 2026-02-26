#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
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
#include <iomanip>
#include <cstdint>
#include <set>
#include <array>
#include <deque>

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
        this->declare_parameter("max_jerk", 0.0);  // rad/s³, 0 = disabled
        this->declare_parameter("joints", std::vector<std::string>{});
        
        int baud_rate = this->get_parameter("baud_rate").as_int();
        control_frequency_ = this->get_parameter("control_frequency").as_double();
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
        
        // Setup ARM publishers/subscribers/services
        RCLCPP_INFO(this->get_logger(), "Setting up ARM publishers/subscribers/services");
        arm_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/state", 10);
        arm_command_state_pub_ = this->create_publisher<sensor_msgs::msg::JointState>("/mars/arm/command_state", 10);
        arm_status_pub_ = this->create_publisher<maurice_msgs::msg::ArmStatus>("/mars/arm/status", 10);
        // Use best_effort QoS to match UDP receiver publisher for low-latency teleoperation
        auto cmd_qos = rclcpp::QoS(1).best_effort();
        // TODO: make sure it works with calibration bag
        arm_command_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
            "/mars/arm/commands2", cmd_qos,
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
        
        arm_fix_error_service_ = this->create_service<std_srvs::srv::Trigger>(
            "/mars/arm/fix_error",
            std::bind(&MauriceArmNode::armFixErrorCallback, this, std::placeholders::_1, std::placeholders::_2),
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
        auto [initial_positions, initial_velocities, initial_loads] = robot_->readState();
        (void)initial_loads;
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
    // x330 motors (XL330, XC330) have current control hw (addr 38/102, modes 0/5)
    // x430 motors do not. addr 38 max = 1750 mA for all x330.
    static constexpr int kX330MaxCurrentLimit = 1750;
    
    static bool isX330(const std::string& motor_type) {
        return motor_type.find("330") != std::string::npos;
    }
    
    // Gain profile: 5 PID+FF values per joint
    struct GainProfile {
        int kp = 0, ki = 0, kd = 0, ff1 = 0, ff2 = 0;
        bool operator==(const GainProfile& o) const {
            return kp == o.kp && ki == o.ki && kd == o.kd && ff1 == o.ff1 && ff2 == o.ff2;
        }
        bool operator!=(const GainProfile& o) const { return !(*this == o); }
    };
    
    // Gain mode: SCHEDULED = interpolate near/far by extension, TELEOP = flat teleop gains
    enum class GainMode { SCHEDULED, TELEOP };

    static GainProfile parseGainsArray(const std::vector<int64_t>& arr) {
        constexpr int kMaxGain = 16383;
        GainProfile g;
        if (arr.size() >= 1) g.kp  = std::clamp(static_cast<int>(arr[0]), 0, kMaxGain);
        if (arr.size() >= 2) g.ki  = std::clamp(static_cast<int>(arr[1]), 0, kMaxGain);
        if (arr.size() >= 3) g.kd  = std::clamp(static_cast<int>(arr[2]), 0, kMaxGain);
        if (arr.size() >= 4) g.ff1 = std::clamp(static_cast<int>(arr[3]), 0, kMaxGain);
        if (arr.size() >= 5) g.ff2 = std::clamp(static_cast<int>(arr[4]), 0, kMaxGain);
        return g;
    }
    
    void loadJointConfigs(const std::vector<std::string>& joint_names) {
        RCLCPP_INFO(this->get_logger(), "Loading %zu joint configurations...", joint_names.size());
        
        for (size_t i = 0; i < joint_names.size(); ++i) {
            const std::string& jn = joint_names[i];  // e.g. "joint_1"
            JointConfig config;
            
            // Declare and read sub-parameters (nav2 style: joint_N.param)
            this->declare_parameter(jn + ".servo_id", 0);
            this->declare_parameter(jn + ".position_limits", std::vector<double>{});
            this->declare_parameter(jn + ".pwm_limit", 885);
            this->declare_parameter(jn + ".control_mode", 3);
            this->declare_parameter(jn + ".motor_type", std::string(""));
            this->declare_parameter(jn + ".current_limit", 0);
            this->declare_parameter(jn + ".homing_offset", 0);
            this->declare_parameter(jn + ".profile_velocity", 0);
            this->declare_parameter(jn + ".profile_acceleration", 0);
            this->declare_parameter(jn + ".gains_near", std::vector<int64_t>{});
            this->declare_parameter(jn + ".gains_far", std::vector<int64_t>{});
            this->declare_parameter(jn + ".gains_teleop", std::vector<int64_t>{});
            
            config.servo_id = static_cast<int>(this->get_parameter(jn + ".servo_id").as_int());
            auto pos_limits = this->get_parameter(jn + ".position_limits").as_double_array();
            if (pos_limits.size() >= 2) {
                config.min_pos_rad = pos_limits[0];
                config.max_pos_rad = pos_limits[1];
            }
            config.pwm_limit = static_cast<int>(this->get_parameter(jn + ".pwm_limit").as_int());
            config.control_mode = static_cast<int>(this->get_parameter(jn + ".control_mode").as_int());
            config.motor_type = this->get_parameter(jn + ".motor_type").as_string();
            
            // Validate motor_type vs control_mode
            if (!config.motor_type.empty() && !isX330(config.motor_type) &&
                (config.control_mode == 0 || config.control_mode == 5)) {
                throw std::runtime_error(
                    jn + " (" + config.motor_type +
                    "): control_mode " + std::to_string(config.control_mode) +
                    " requires current hw — only x330 motors support modes 0/5");
            }
            
            // Current limit: x330 defaults to hw max, x430 has no current hw
            int cl = static_cast<int>(this->get_parameter(jn + ".current_limit").as_int());
            if (isX330(config.motor_type)) {
                config.current_limit = (cl > 0) ? cl : kX330MaxCurrentLimit;
                if (config.current_limit > kX330MaxCurrentLimit) {
                    throw std::runtime_error(jn + ": current_limit out of range [1, " +
                        std::to_string(kX330MaxCurrentLimit) + "]");
                }
            } else if (cl > 0) {
                throw std::runtime_error(jn + " (" + config.motor_type +
                    "): current_limit not supported — no current control hw");
            }
            
            config.homing_offset = static_cast<int>(this->get_parameter(jn + ".homing_offset").as_int());
            config.profile_velocity = static_cast<int>(this->get_parameter(jn + ".profile_velocity").as_int());
            config.profile_acceleration = static_cast<int>(this->get_parameter(jn + ".profile_acceleration").as_int());
            
            RCLCPP_INFO(this->get_logger(), "Joint %zu (%s): servo_id=%d, motor=%s, limits=[%.3f, %.3f] rad, pwm=%d, mode=%d, current=%d",
                i + 1, jn.c_str(), config.servo_id,
                config.motor_type.empty() ? "(unset)" : config.motor_type.c_str(),
                config.min_pos_rad, config.max_pos_rad, config.pwm_limit, config.control_mode, config.current_limit);
            if (config.homing_offset != 0)
                RCLCPP_INFO(this->get_logger(), "  Homing offset: %d", config.homing_offset);
            
            // Gains: gains_near = [kp, ki, kd, ff1, ff2], gains_far = [] means same as near
            auto near_arr = this->get_parameter(jn + ".gains_near").as_integer_array();
            GainProfile near_gains = parseGainsArray(near_arr);
            
            GainProfile far_gains = near_gains;  // default: far == near (gain scheduling no-ops)
            try {
                auto far_arr = this->get_parameter(jn + ".gains_far").as_integer_array();
                if (!far_arr.empty()) {
                    far_gains = parseGainsArray(far_arr);
                }
            } catch (...) {}  // empty [] in YAML may not parse as integer array
            
            GainProfile teleop_gains{};  // teleop gains (November tuning)
            bool has_teleop_gains = false;
            try {
                auto teleop_arr = this->get_parameter(jn + ".gains_teleop").as_integer_array();
                if (!teleop_arr.empty()) {
                    teleop_gains = parseGainsArray(teleop_arr);
                    has_teleop_gains = true;
                }
            } catch (...) {}
            
            // Set joint config gains from near (initial operating gains)
            config.kp  = near_gains.kp;
            config.ki  = near_gains.ki;
            config.kd  = near_gains.kd;
            config.ff1 = near_gains.ff1;
            config.ff2 = near_gains.ff2;
            
            // Store gain scheduling profiles
            gs_near_[i] = near_gains;
            gs_far_[i]  = far_gains;
            gs_teleop_[i] = has_teleop_gains ? teleop_gains : near_gains;
            gs_last_applied_[i] = near_gains;
            
            RCLCPP_INFO(this->get_logger(), "  Gains near: [%d, %d, %d, %d, %d]  far: [%d, %d, %d, %d, %d]%s",
                near_gains.kp, near_gains.ki, near_gains.kd, near_gains.ff1, near_gains.ff2,
                far_gains.kp, far_gains.ki, far_gains.kd, far_gains.ff1, far_gains.ff2,
                (near_gains != far_gains) ? "  (gain scheduling active)" : "");
            if (has_teleop_gains)
                RCLCPP_INFO(this->get_logger(), "  Gains teleop: [%d, %d, %d, %d, %d]",
                    teleop_gains.kp, teleop_gains.ki, teleop_gains.kd, teleop_gains.ff1, teleop_gains.ff2);
            if (config.profile_velocity > 0 || config.profile_acceleration > 0)
                RCLCPP_INFO(this->get_logger(), "  Profile: vel=%d, accel=%d", config.profile_velocity, config.profile_acceleration);
            
            // Head-specific config for joint 7 (index 6)
            if (i == 6) {
                this->declare_parameter(jn + ".head_config.ai_position_deg", 0.0);
                this->declare_parameter(jn + ".head_config.direction_reversed", false);
                
                config.head_ai_position_deg = this->get_parameter(jn + ".head_config.ai_position_deg").as_double();
                config.head_direction_reversed = this->get_parameter(jn + ".head_config.direction_reversed").as_bool();
                
                constexpr double RAD_TO_DEG = 180.0 / M_PI;
                if (config.head_direction_reversed) {
                    config.head_min_angle_deg = -config.max_pos_rad * RAD_TO_DEG;
                    config.head_max_angle_deg = -config.min_pos_rad * RAD_TO_DEG;
                } else {
                    config.head_min_angle_deg = config.min_pos_rad * RAD_TO_DEG;
                    config.head_max_angle_deg = config.max_pos_rad * RAD_TO_DEG;
                }
                
                RCLCPP_INFO(this->get_logger(), "  Head config: range=[%.1f, %.1f] deg, AI pos=%.1f deg, reversed=%s",
                    config.head_min_angle_deg, config.head_max_angle_deg, config.head_ai_position_deg,
                    config.head_direction_reversed ? "true" : "false");
            }
            
            joint_configs_.push_back(config);
        }
        RCLCPP_INFO(this->get_logger(), "Loaded %zu joint configurations", joint_configs_.size());
    }
    
    rcl_interfaces::msg::SetParametersResult onParameterChange(
        const std::vector<rclcpp::Parameter>& parameters) {
        
        rcl_interfaces::msg::SetParametersResult result;
        result.successful = true;
        
        // Collect which joints had PID or profile changes
        std::set<int> pid_changed_joints;
        std::set<int> profile_changed_joints;
        
        for (const auto& param : parameters) {
            const std::string& name = param.get_name();
            
            // max_jerk is read on-the-fly from the parameter server each trajectory,
            // so just log the change — no servo write needed.
            if (name == "max_jerk") {
                RCLCPP_INFO(this->get_logger(), "Hot-reload: max_jerk = %.1f rad/s³", param.as_double());
                continue;
            }
            
            // Match pattern: joint_N.<suffix>
            if (name.size() >= 8 && name.substr(0, 6) == "joint_") {
                size_t dot = name.find('.', 6);
                if (dot == std::string::npos) continue;
                
                int joint_num = 0;
                try {
                    joint_num = std::stoi(name.substr(6, dot - 6));
                } catch (...) { continue; }
                
                if (joint_num < 1 || joint_num > static_cast<int>(joint_configs_.size())) continue;
                int ji = joint_num - 1;
                
                std::string suffix = name.substr(dot + 1);
                
                if (suffix == "gains_near") {
                    auto arr = param.as_integer_array();
                    GainProfile g = parseGainsArray(arr);
                    gs_near_[ji] = g;
                    // Update current joint config with near gains
                    joint_configs_[ji].kp  = g.kp;
                    joint_configs_[ji].ki  = g.ki;
                    joint_configs_[ji].kd  = g.kd;
                    joint_configs_[ji].ff1 = g.ff1;
                    joint_configs_[ji].ff2 = g.ff2;
                    pid_changed_joints.insert(joint_num);
                    RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_near = [%d, %d, %d, %d, %d]",
                        joint_num, g.kp, g.ki, g.kd, g.ff1, g.ff2);
                } else if (suffix == "gains_far") {
                    try {
                        auto arr = param.as_integer_array();
                        gs_far_[ji] = arr.empty() ? gs_near_[ji] : parseGainsArray(arr);
                    } catch (...) {
                        gs_far_[ji] = gs_near_[ji];  // empty [] in YAML
                    }
                    RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_far = [%d, %d, %d, %d, %d]",
                        joint_num, gs_far_[ji].kp, gs_far_[ji].ki, gs_far_[ji].kd, gs_far_[ji].ff1, gs_far_[ji].ff2);
                } else if (suffix == "gains_teleop") {
                    try {
                        auto arr = param.as_integer_array();
                        gs_teleop_[ji] = arr.empty() ? gs_near_[ji] : parseGainsArray(arr);
                    } catch (...) {
                        gs_teleop_[ji] = gs_near_[ji];
                    }
                    RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.gains_teleop = [%d, %d, %d, %d, %d]",
                        joint_num, gs_teleop_[ji].kp, gs_teleop_[ji].ki, gs_teleop_[ji].kd, gs_teleop_[ji].ff1, gs_teleop_[ji].ff2);
                } else if (suffix == "profile_velocity") {
                    joint_configs_[ji].profile_velocity = static_cast<int>(param.as_int());
                    profile_changed_joints.insert(joint_num);
                    RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.profile_velocity = %d",
                        joint_num, joint_configs_[ji].profile_velocity);
                } else if (suffix == "profile_acceleration") {
                    joint_configs_[ji].profile_acceleration = static_cast<int>(param.as_int());
                    profile_changed_joints.insert(joint_num);
                    RCLCPP_INFO(this->get_logger(), "Hot-reload: joint_%d.profile_acceleration = %d",
                        joint_num, joint_configs_[ji].profile_acceleration);
                }
            }
        }
        
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            // SyncWrite PID for changed joints
            if (!pid_changed_joints.empty()) {
                std::vector<std::tuple<int, int, int, int, int, int>> pid_data;
                for (int jn : pid_changed_joints) {
                    const auto& c = joint_configs_[jn - 1];
                    pid_data.emplace_back(c.servo_id, c.ff2, c.ff1, c.kd, c.ki, c.kp);
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

    // Retry a servo operation up to max_retries times with delay between attempts
    template<typename Func>
    void retryServoOp(int servo_id, const char* op_name, Func&& fn, int max_retries = 3) {
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

    // Configure a single servo by ID — shared by configureServosLocked and armFixErrorCallback
    void configureServoByIdLocked(int servo_id, bool enable_torque = true) {
        // Find the config for this servo
        const JointConfig* config_ptr = nullptr;
        size_t joint_index = 0;
        for (size_t i = 0; i < joint_configs_.size(); ++i) {
            if (joint_configs_[i].servo_id == servo_id) {
                config_ptr = &joint_configs_[i];
                joint_index = i;
                break;
            }
        }
        if (!config_ptr) {
            throw std::runtime_error("No config found for servo ID " + std::to_string(servo_id));
        }
        const auto& config = *config_ptr;
        
        RCLCPP_INFO(this->get_logger(), "Configuring servo %d (%s)", config.servo_id,
            config.motor_type.empty() ? "unknown" : config.motor_type.c_str());
        
        retryServoOp(config.servo_id, "disableTorque", [&]{ dynamixel_->disableTorque(config.servo_id); });
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        
        dynamixel_->setReturnDelayTime(config.servo_id, 100);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        
        int min_encoder = static_cast<int>((config.min_pos_rad / (2 * M_PI)) * 4096 + 2048);
        int max_encoder = static_cast<int>((config.max_pos_rad / (2 * M_PI)) * 4096 + 2048);
        dynamixel_->setMinPositionLimit(config.servo_id, min_encoder);
        dynamixel_->setMaxPositionLimit(config.servo_id, max_encoder);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        
        dynamixel_->setPwmLimit(config.servo_id, config.pwm_limit);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        
        if (config.current_limit > 0) {
            dynamixel_->setCurrentLimit(config.servo_id, config.current_limit);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        OperatingMode mode;
        switch (config.control_mode) {
            case 0:  mode = OperatingMode::CURRENT; break;
            case 1:  mode = OperatingMode::VELOCITY; break;
            case 3:  mode = OperatingMode::POSITION; break;
            case 4:  mode = OperatingMode::EXTENDED_POSITION; break;
            case 5:  mode = OperatingMode::CURRENT_CONTROLLED_POSITION; break;
            case 16: mode = OperatingMode::PWM; break;
            default: throw std::runtime_error("Servo " + std::to_string(config.servo_id) + ": invalid control_mode");
        }
        dynamixel_->setOperatingMode(config.servo_id, mode);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        
        if (config.homing_offset != 0) {
            dynamixel_->setHomeOffset(config.servo_id, config.homing_offset);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        // Write PID gains for this servo
        std::vector<std::tuple<int, int, int, int, int, int>> pid_data;
        pid_data.emplace_back(config.servo_id, config.ff2, config.ff1, config.kd, config.ki, config.kp);
        dynamixel_->syncWritePID(pid_data);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        
        if (config.profile_velocity > 0) {
            dynamixel_->setProfileVelocity(config.servo_id, config.profile_velocity);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (config.profile_acceleration > 0) {
            dynamixel_->setProfileAcceleration(config.servo_id, config.profile_acceleration);
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        
        // Reset gain scheduling tracking for this joint
        gs_last_applied_[joint_index] = gs_near_[joint_index];
        
        if (enable_torque) {
            RCLCPP_INFO(this->get_logger(), "  Enabling torque on servo %d", config.servo_id);
            retryServoOp(config.servo_id, "enableTorque", [&]{ dynamixel_->enableTorque(config.servo_id); });
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        RCLCPP_INFO(this->get_logger(), "Servo %d configured and torque %s",
            config.servo_id, enable_torque ? "enabled" : "disabled");
    }

    void configureServosLocked(bool enable_torque = true) {
        RCLCPP_INFO(this->get_logger(), "Configuring all 7 servos...");
        
        // Configure all servos (IDs 1-7) uniformly using shared per-servo logic
        for (const auto& config : joint_configs_) {
          try {
            configureServoByIdLocked(config.servo_id, enable_torque);
          } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to configure servo %d, skipping: %s",
                config.servo_id, e.what());
          }
        }
        
        // Move head to default position
        try {
            RCLCPP_INFO(this->get_logger(), "Moving head to default position (0.0 deg)");
            moveHeadToAngleLocked(0.0);
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to move head to default position: %s", e.what());
        }
    }
    
    // Timer callback for unified control loop (replaces thread-based loop)
    void controlTimerCallback() {
        // ts[0..8]: loop_start, lock_acquired, read_done, effort_done,
        //           pub_arm_done, pub_head_js_done, gain_sched_done, cmd_write_done, loop_end
        std::array<std::chrono::steady_clock::time_point, 9> ts;
        ts[0] = std::chrono::steady_clock::now();
        ts.fill(ts[0]);
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            ts[1] = std::chrono::steady_clock::now();
            
            // ========== READ STATE (position + velocity + load in one bulk read) ==========
            auto [positions, velocities, loads] = robot_->readState();
            ts[2] = std::chrono::steady_clock::now();
            
            // Convert raw load/current to effort values
            // current_limit > 0 means x330 (addr 126 = mA), else x430 (addr 126 = 0.1% load)
            std::vector<double> efforts;
            for (size_t j = 0; j < loads.size() && j < joint_configs_.size(); ++j) {
                if (joint_configs_[j].current_limit > 0) {
                    efforts.push_back(static_cast<double>(loads[j]));        // mA
                } else {
                    efforts.push_back(static_cast<double>(loads[j]) / 10.0); // %
                }
            }
            ts[3] = std::chrono::steady_clock::now();
            
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
            ts[4] = std::chrono::steady_clock::now();
            
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
            ts[5] = std::chrono::steady_clock::now();
            
            // ========== PERIODIC GAIN DUMP (every 2s) ==========
            RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                "Gains: J1[P=%d I=%d D=%d F1=%d F2=%d] J2[P=%d I=%d D=%d F1=%d F2=%d] "
                "J3[P=%d I=%d D=%d F1=%d F2=%d] J4[P=%d I=%d D=%d F1=%d F2=%d] "
                "J5[P=%d I=%d D=%d F1=%d F2=%d] J6[P=%d I=%d D=%d F1=%d F2=%d] "
                "J7[P=%d I=%d D=%d F1=%d F2=%d]",
                joint_configs_[0].kp, joint_configs_[0].ki, joint_configs_[0].kd, joint_configs_[0].ff1, joint_configs_[0].ff2,
                joint_configs_[1].kp, joint_configs_[1].ki, joint_configs_[1].kd, joint_configs_[1].ff1, joint_configs_[1].ff2,
                joint_configs_[2].kp, joint_configs_[2].ki, joint_configs_[2].kd, joint_configs_[2].ff1, joint_configs_[2].ff2,
                joint_configs_[3].kp, joint_configs_[3].ki, joint_configs_[3].kd, joint_configs_[3].ff1, joint_configs_[3].ff2,
                joint_configs_[4].kp, joint_configs_[4].ki, joint_configs_[4].kd, joint_configs_[4].ff1, joint_configs_[4].ff2,
                joint_configs_[5].kp, joint_configs_[5].ki, joint_configs_[5].kd, joint_configs_[5].ff1, joint_configs_[5].ff2,
                joint_configs_[6].kp, joint_configs_[6].ki, joint_configs_[6].kd, joint_configs_[6].ff1, joint_configs_[6].ff2);
            
            // ========== GAIN SCHEDULING ==========
            // TELEOP mode: use flat teleop gains (no extension-based interpolation)
            // SCHEDULED mode: interpolate near/far by arm extension (gravity compensation)
            if (++gs_cycle_counter_ >= kGainScheduleInterval) {
                gs_cycle_counter_ = 0;
                
                bool gs_changed = false;
                std::vector<std::tuple<int, int, int, int, int, int>> gs_pid_data;
                
                if (gain_mode_ == GainMode::TELEOP) {
                    // TELEOP: apply flat teleop gains for joints 1-6
                    for (int i = 0; i < 6; i++) {
                        const GainProfile& target = gs_teleop_[i];
                        if (target != gs_last_applied_[i]) {
                            gs_changed = true;
                            gs_last_applied_[i] = target;
                            joint_configs_[i].kp  = target.kp;
                            joint_configs_[i].ki  = target.ki;
                            joint_configs_[i].kd  = target.kd;
                            joint_configs_[i].ff1 = target.ff1;
                            joint_configs_[i].ff2 = target.ff2;
                        }
                        gs_pid_data.emplace_back(joint_configs_[i].servo_id, target.ff2, target.ff1, target.kd, target.ki, target.kp);
                    }
                    if (gs_changed) {
                        auto pid_start = std::chrono::steady_clock::now();
                        dynamixel_->syncWritePID(gs_pid_data);
                        auto pid_us = std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - pid_start).count();
                        timing_stats_[8].add(pid_us);
                        RCLCPP_INFO(this->get_logger(),
                            "TELEOP gains applied | J1 P=%d I=%d D=%d | J2 P=%d I=%d D=%d | J3 P=%d I=%d D=%d | J4 P=%d I=%d D=%d",
                            gs_teleop_[0].kp, gs_teleop_[0].ki, gs_teleop_[0].kd,
                            gs_teleop_[1].kp, gs_teleop_[1].ki, gs_teleop_[1].kd,
                            gs_teleop_[2].kp, gs_teleop_[2].ki, gs_teleop_[2].kd,
                            gs_teleop_[3].kp, gs_teleop_[3].ki, gs_teleop_[3].kd);
                    }
                } else {
                    // SCHEDULED: interpolate near/far by arm extension for joints 1-4
                    // Compute extension via 2D planar FK (shoulder XZ plane, Y-axis pitch joints)
                    constexpr double L2_x = 0.02825, L2_z = 0.12125;  // joint2 → joint3
                    constexpr double L3_x = 0.1375,  L3_z = 0.0045;   // joint3 → joint4
                    constexpr double L45_x = 0.110838;                  // joint4 → EE
                    constexpr double kMaxReach = 0.37291;
                    
                    double q2 = positions_rad[1], q3 = positions_rad[2], q4 = positions_rad[3];
                    double a2 = q2, a23 = q2 + q3, a234 = q2 + q3 + q4;
                    
                    double ee_x = L2_x*std::cos(a2)  + L2_z*std::sin(a2)
                                + L3_x*std::cos(a23) + L3_z*std::sin(a23)
                                + L45_x*std::cos(a234);
                    
                    double horiz_reach = std::abs(ee_x);
                    double extension_linear = std::clamp((horiz_reach / kMaxReach - 0.1) / 0.9, 0.0, 1.0);
                    double extension = extension_linear * extension_linear;
                    
                    for (int i = 0; i < 4; i++) {
                        GainProfile interp;
                        interp.kp = gs_near_[i].kp + static_cast<int>(extension * (gs_far_[i].kp - gs_near_[i].kp));
                        interp.ki = gs_near_[i].ki + static_cast<int>(extension * (gs_far_[i].ki - gs_near_[i].ki));
                        interp.kd = gs_near_[i].kd + static_cast<int>(extension * (gs_far_[i].kd - gs_near_[i].kd));
                        interp.ff1 = gs_near_[i].ff1 + static_cast<int>(extension * (gs_far_[i].ff1 - gs_near_[i].ff1));
                        interp.ff2 = gs_near_[i].ff2 + static_cast<int>(extension * (gs_far_[i].ff2 - gs_near_[i].ff2));
                        
                        if (interp != gs_last_applied_[i]) {
                            gs_changed = true;
                            gs_last_applied_[i] = interp;
                            joint_configs_[i].kp  = interp.kp;
                            joint_configs_[i].ki  = interp.ki;
                            joint_configs_[i].kd  = interp.kd;
                            joint_configs_[i].ff1 = interp.ff1;
                            joint_configs_[i].ff2 = interp.ff2;
                        }
                        gs_pid_data.emplace_back(joint_configs_[i].servo_id, interp.ff2, interp.ff1, interp.kd, interp.ki, interp.kp);
                    }
                    if (gs_changed) {
                        auto pid_start = std::chrono::steady_clock::now();
                        dynamixel_->syncWritePID(gs_pid_data);
                        auto pid_us = std::chrono::duration_cast<std::chrono::microseconds>(
                            std::chrono::steady_clock::now() - pid_start).count();
                        timing_stats_[8].add(pid_us);
                        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 500,
                            "GainSched ext=%.2f (h=%.3fm) | J1 P=%d D=%d | J2 P=%d D=%d | J3 P=%d D=%d | J4 P=%d D=%d",
                            extension, horiz_reach,
                            gs_last_applied_[0].kp, gs_last_applied_[0].kd,
                            gs_last_applied_[1].kp, gs_last_applied_[1].kd,
                            gs_last_applied_[2].kp, gs_last_applied_[2].kd,
                            gs_last_applied_[3].kp, gs_last_applied_[3].kd);
                    }
                }
            }
            ts[6] = std::chrono::steady_clock::now();
            
            // ========== COMMAND PASS-THROUGH (runs at 200Hz) ==========
            // Sends latest received target directly to servos.
            // Smoothing is handled by servo Profile Velocity/Acceleration.
            {
                bool has_cmd = false;
                std::array<double, 6> result_rad{};
                
                {
                    std::lock_guard<std::mutex> arm_lock(arm_command_mutex_);
                    if (has_target_) {
                        result_rad = latest_target_;
                        has_cmd = true;
                    }
                }
                
                if (has_cmd) {
                    std::vector<double> cmd_vec(result_rad.begin(), result_rad.end());
                    std::vector<int> arm_encoder = applyLimitsAndConvertToEncoder(cmd_vec);
                    
                    std::vector<int> full_command(7);
                    std::copy(arm_encoder.begin(), arm_encoder.end(), full_command.begin());
                    {
                        std::lock_guard<std::mutex> head_lock(head_command_mutex_);
                        full_command[6] = latest_head_command_;
                        has_head_command_ = false;
                    }
                    
                    robot_->setGoalPos(full_command);
                    
                    // Publish smoothed command (in radians, external convention)
                    sensor_msgs::msg::JointState cmd_msg;
                    cmd_msg.header.stamp = this->now();
                    cmd_msg.name = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
                    cmd_msg.position.resize(6);
                    for (int i = 0; i < 6; ++i) {
                        double rad = ((full_command[i] - 2048) * 2 * M_PI) / 4096.0;
                        if (i == 1 || i == 2 || i == 3 || i == 5) rad = -rad;
                        cmd_msg.position[i] = rad;
                    }
                    arm_command_state_pub_->publish(cmd_msg);
                } else if (has_head_command_.load()) {
                    // No arm commands yet — just handle head
                    std::lock_guard<std::mutex> head_lock(head_command_mutex_);
                    int head_enc = latest_head_command_;
                    has_head_command_ = false;
                    std::vector<int> full_command(latest_arm_command_);
                    full_command.resize(7);
                    full_command[6] = head_enc;
                    robot_->setGoalPos(full_command);
                }
            }
            ts[7] = std::chrono::steady_clock::now();
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Control timer error: %s", e.what());
        }

        ts[8] = std::chrono::steady_clock::now();
        recordLoopTiming(ts);
    }
    
    void recordLoopTiming(std::array<std::chrono::steady_clock::time_point, 9>& ts) {
        // Fix up timestamps if exception caused early exit
        for (size_t i = 1; i < ts.size(); ++i)
            if (ts[i] < ts[i - 1]) ts[i] = ts[i - 1];

        // Compute per-section durations (indices 0=total, 1-7=sections, 9=misc)
        std::array<long, 10> us{};
        us[0] = std::chrono::duration_cast<std::chrono::microseconds>(ts[8] - ts[0]).count();
        for (size_t i = 1; i <= 7; ++i)
            us[i] = std::chrono::duration_cast<std::chrono::microseconds>(ts[i] - ts[i - 1]).count();
        us[9] = std::chrono::duration_cast<std::chrono::microseconds>(ts[8] - ts[7]).count();

        // Accumulate (skip index 8 = event-based syncWritePID)
        for (size_t i = 0; i < timing_stats_.size(); ++i)
            if (i != 8) timing_stats_[i].add(us[i]);

        // Log avg|max and percentage breakdown every 2s
        long avg_total = timing_stats_[0].avg();

        std::ostringstream msg;
        msg << "LoopTiming(us avg|max, n=" << timing_stats_[0].samples << "):";
        for (size_t i = 0; i < timing_stats_.size(); ++i)
            msg << ' ' << timing_stats_[i].name << '=' << timing_stats_[i].avg() << '|' << timing_stats_[i].max_us;
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "%s", msg.str().c_str());

        std::ostringstream pct;
        pct << std::fixed << std::setprecision(1) << "LoopTiming(%):";
        for (size_t i : {1u, 2u, 3u, 4u, 5u, 6u, 7u, 9u})
            pct << ' ' << timing_stats_[i].name << '='
                << (avg_total > 0 ? 100.0 * timing_stats_[i].avg() / avg_total : 0.0);
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "%s", pct.str().c_str());

        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
            "SerialBus(us): readState_txrx=%ld  write_txrx=%ld",
            robot_->last_read_txrx_us, robot_->last_write_txrx_us);
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
    
    void armCommandCallback(const sensor_msgs::msg::JointState::SharedPtr msg) {
        try {
            if (msg->position.size() != 6) {
                RCLCPP_ERROR(this->get_logger(), "Action size must match number of servos. Expected 6, got %zu", msg->position.size());
                return;
            }
            
            std::lock_guard<std::mutex> lock(arm_command_mutex_);
            for (int i = 0; i < 6; ++i)
                latest_target_[i] = msg->position[i];
            has_target_ = true;
            
            // Switch to teleop gains when streaming commands arrive
            if (gain_mode_ != GainMode::TELEOP) {
                gain_mode_ = GainMode::TELEOP;
                RCLCPP_INFO(this->get_logger(), "Gain mode -> TELEOP (streaming commands detected)");
            }
            
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
    
    // Fix only the servo(s) that have a hardware error: reboot, reconfigure, torque on
    void armFixErrorCallback(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
        RCLCPP_INFO(this->get_logger(), "Service called: /mars/arm/fix_error");
        try {
            std::lock_guard<std::mutex> lock(dynamixel_mutex_);
            
            // Scan all servos and collect those with hardware errors
            std::vector<int> error_servo_ids;
            for (const auto& config : joint_configs_) {
                uint8_t hw_status = dynamixel_->readHardwareErrorStatus(config.servo_id);
                if (hw_status != 0) {
                    RCLCPP_WARN(this->get_logger(), "Servo %d has hardware error: %s",
                        config.servo_id, describeHardwareError(hw_status, config.servo_id).c_str());
                    error_servo_ids.push_back(config.servo_id);
                }
            }
            
            // Build JSON array of error IDs for machine-readable response
            json error_ids_json = json::array();
            for (int id : error_servo_ids) {
                error_ids_json.push_back(id);
            }
            
            if (error_servo_ids.empty()) {
                json result;
                result["error_ids"] = error_ids_json;
                result["status"] = "no_errors";
                response->success = true;
                response->message = result.dump();
                RCLCPP_INFO(this->get_logger(), "No servos have hardware errors");
                return;
            }
            
            // Reboot only the errored servos
            for (int servo_id : error_servo_ids) {
                RCLCPP_INFO(this->get_logger(), "Rebooting servo %d...", servo_id);
                dynamixel_->reboot(servo_id);
            }
            
            // Wait for servos to come back online after reboot
            RCLCPP_INFO(this->get_logger(), "Waiting for rebooted servos to come back online...");
            std::this_thread::sleep_for(std::chrono::milliseconds(1500));
            
            // Reconfigure and torque-on only the rebooted servos
            for (int servo_id : error_servo_ids) {
                RCLCPP_INFO(this->get_logger(), "Reconfiguring servo %d...", servo_id);
                configureServoByIdLocked(servo_id, true);
            }
            
            // Build JSON response with error IDs and status
            json result;
            result["error_ids"] = error_ids_json;
            result["status"] = "fixed";
            
            response->success = true;
            response->message = result.dump();
            RCLCPP_INFO(this->get_logger(), "Fixed %zu servo(s): %s",
                error_servo_ids.size(), response->message.c_str());
        } catch (const std::exception& e) {
            response->success = false;
            json err_result;
            err_result["error_ids"] = json::array();
            err_result["status"] = std::string("error: ") + e.what();
            response->message = err_result.dump();
            RCLCPP_ERROR(this->get_logger(), "Failed to fix error: %s", e.what());
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
     * Compute a quintic (fifth-order) smootherstep trajectory between start and goal.
     * Uses profile: ratio = 6t^5 - 15t^4 + 10t^3
     * Zero velocity AND zero acceleration at both endpoints → continuous jerk.
     * Trade-off: 25% higher peak velocity at midpoint vs cubic smoothstep.
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
                    "Jerk limit %.1f rad/s³: extending trajectory %.2fs → %.2fs (Δθ_max=%.3f rad)",
                    max_jerk, duration, min_duration, max_delta);
                duration = min_duration;
            }
        }
        
        int num_steps = static_cast<int>(duration / dt);
        if (num_steps < 1) num_steps = 1;
        
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
        // Switch to scheduled gains for planned trajectories
        if (gain_mode_ != GainMode::SCHEDULED) {
            gain_mode_ = GainMode::SCHEDULED;
            RCLCPP_INFO(this->get_logger(), "Gain mode -> SCHEDULED (trajectory execution)");
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
        
        // Use simple cubic spline planning (fast, smooth trajectory)
        RCLCPP_INFO(this->get_logger(), "Planning with cubic spline for 6-DOF arm (including gripper)...");
        const double dt = 1.0 / this->get_parameter("trajectory_rate_hz").as_double();
        auto interpolated_trajectory = computeCubicSplineTrajectory(current_positions, target_positions, trajectory_time, dt);
        
        if (interpolated_trajectory.empty()) {
            RCLCPP_ERROR(this->get_logger(), "Cubic spline trajectory computation failed");
            return false;
        }
        
        // Detect if jerk limiting extended the duration
        double actual_duration = (interpolated_trajectory.size() - 1) * dt;
        if (actual_duration > trajectory_time * 1.01) {
            RCLCPP_WARN(this->get_logger(),
                "Jerk-limited: requested %.2fs but executing %.2fs (+%.0f%%)",
                trajectory_time, actual_duration,
                100.0 * (actual_duration - trajectory_time) / trajectory_time);
        }
        
        RCLCPP_INFO(this->get_logger(), "Executing trajectory with %zu waypoints over %.2f seconds", 
                    interpolated_trajectory.size(), actual_duration);
        
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
        std::string motor_type;  // e.g. "XC330-M288", "XL430-W250" — "330" = has current hw
        double min_pos_rad;
        double max_pos_rad;
        int pwm_limit;
        int current_limit = 0;
        int homing_offset = 0;
        int control_mode;
        int kp, ki, kd;
        int ff1 = 0;  // Velocity feedforward gain (addr 78, range 0-16383)
        int ff2 = 0;  // Acceleration feedforward gain (addr 76, range 0-16383)
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
    rclcpp::Publisher<sensor_msgs::msg::JointState>::SharedPtr arm_command_state_pub_;
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr arm_command_sub_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_on_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_torque_off_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_reboot_service_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr arm_fix_error_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJS>::SharedPtr arm_goto_js_service_;
    rclcpp::Service<maurice_msgs::srv::GotoJSTrajectory>::SharedPtr arm_goto_js_traj_service_;
    sensor_msgs::msg::JointState arm_state_msg_;  // 6-joint message for /mars/arm/state
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
    struct TimingAccumulator {
        const char* name = "";
        long sum_us = 0;
        long max_us = 0;
        long samples = 0;

        void add(long us) {
            sum_us += us;
            max_us = std::max(max_us, us);
            ++samples;
        }

        long avg() const {
            return samples > 0 ? (sum_us / samples) : 0;
        }
    };

    static constexpr int kGainScheduleInterval = 20;  // control cycles between updates (~200ms at 100Hz)
    std::array<GainProfile, 7> gs_near_;          // near profiles for all joints
    std::array<GainProfile, 7> gs_far_;           // far profiles (== near when gain scheduling disabled for that joint)
    std::array<GainProfile, 7> gs_teleop_;        // teleop profiles (November tuning, for teleoperation use)
    std::array<GainProfile, 7> gs_last_applied_;  // last gains written (for change detection)
    int gs_cycle_counter_ = 0;
    GainMode gain_mode_{GainMode::SCHEDULED};     // starts in scheduled mode, switches to TELEOP on streaming commands

    // Control loop timing instrumentation (throttled detailed breakdown)
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

