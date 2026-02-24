#include "manipulation/recorder_node.hpp"

#include <filesystem>
#include <fstream>
#include <ctime>
#include <iomanip>
#include <sstream>
#include <thread>
#include <hdf5.h>
#include <nlohmann/json.hpp>

namespace fs = std::filesystem;

namespace manipulation {

RecorderNode::RecorderNode()
    : Node("recorder_node"),
      state_(State::IDLE),
      episode_count_(0),
      all_topics_received_(false),
      replay_state_(ReplayState::IDLE),
      replay_frame_index_(0),
      replay_total_frames_(0),
      replay_fps_(10.0) {
    
    // Declare parameters
    this->declare_parameter("data_directory", "/path/to/data");
    this->declare_parameter("data_frequency", 10);
    this->declare_parameter("image_topics", std::vector<std::string>{"/camera/image_raw", "/camera/image_processed"});
    this->declare_parameter("arm_state_topic", "/arm/state");
    this->declare_parameter("leader_command_topic", "/leader/command");
    this->declare_parameter("velocity_topic", "/cmd_vel");
    this->declare_parameter("odom_topic", "/odom");
    this->declare_parameter("image_size", std::vector<int64_t>{640, 480});
    this->declare_parameter("max_timesteps", 1800);

    // Get parameter values
    std::string data_dir_param = this->get_parameter("data_directory").as_string();
    // Expand ~ to home directory
    if (!data_dir_param.empty() && data_dir_param[0] == '~') {
        const char* home = std::getenv("HOME");
        if (home) {
            data_directory_ = std::string(home) + data_dir_param.substr(1);
        } else {
            data_directory_ = data_dir_param;
        }
    } else {
        data_directory_ = data_dir_param;
    }
    
    data_frequency_ = this->get_parameter("data_frequency").as_int();
    image_topics_ = this->get_parameter("image_topics").as_string_array();
    arm_state_topic_ = this->get_parameter("arm_state_topic").as_string();
    leader_command_topic_ = this->get_parameter("leader_command_topic").as_string();
    velocity_topic_ = this->get_parameter("velocity_topic").as_string();
    odom_topic_ = this->get_parameter("odom_topic").as_string();
    image_size_ = this->get_parameter("image_size").as_integer_array();
    max_timesteps_ = this->get_parameter("max_timesteps").as_int();

    // Initialize TaskManager
    task_manager_ = std::make_unique<TaskManager>(data_directory_);

    // Add innate-os/skills as an additional skill directory to scan
    const char* home = std::getenv("HOME");
    if (home) {
        std::string innate_os_skills = std::string(home) + "/innate-os/skills";
        task_manager_->add_skill_directory(innate_os_skills);
        RCLCPP_INFO(this->get_logger(), "Added additional skill directory: %s", innate_os_skills.c_str());
    }

    // Initialize latest image storage
    for (const auto& topic : image_topics_) {
        latest_images_[topic] = nullptr;
        topics_received_[topic] = false;
    }
    topics_received_[arm_state_topic_] = false;
    topics_received_[leader_command_topic_] = false;
    topics_received_[velocity_topic_] = false;

    // Create QoS profile for image topics
    rclcpp::QoS image_qos(10);
    image_qos.reliability(rclcpp::ReliabilityPolicy::BestEffort);
    image_qos.history(rclcpp::HistoryPolicy::KeepLast);

    // Create subscribers for image topics
    for (const auto& topic : image_topics_) {
        auto sub = this->create_subscription<sensor_msgs::msg::Image>(
            topic, image_qos,
            [this, topic](const sensor_msgs::msg::Image::SharedPtr msg) {
                this->image_callback(msg, topic);
            });
        image_subs_.push_back(sub);
    }

    // Create other subscribers
    arm_state_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
        arm_state_topic_, 10,
        std::bind(&RecorderNode::arm_state_callback, this, std::placeholders::_1));
    
    leader_command_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
        leader_command_topic_, 10,
        std::bind(&RecorderNode::leader_command_callback, this, std::placeholders::_1));
    
    cmd_vel_sub_ = this->create_subscription<geometry_msgs::msg::Twist>(
        velocity_topic_, 10,
        std::bind(&RecorderNode::cmd_vel_callback, this, std::placeholders::_1));
    
    odom_sub_ = this->create_subscription<nav_msgs::msg::Odometry>(
        odom_topic_, 10,
        std::bind(&RecorderNode::odom_callback, this, std::placeholders::_1));

    // Log subscriptions
    RCLCPP_INFO(this->get_logger(), "Subscribing to image topics:");
    for (const auto& topic : image_topics_) {
        RCLCPP_INFO(this->get_logger(), "  %s", topic.c_str());
    }
    RCLCPP_INFO(this->get_logger(), "Subscribing to arm state topic: %s", arm_state_topic_.c_str());
    RCLCPP_INFO(this->get_logger(), "Subscribing to leader command topic: %s", leader_command_topic_.c_str());
    RCLCPP_INFO(this->get_logger(), "Subscribing to velocity topic: %s", velocity_topic_.c_str());
    RCLCPP_INFO(this->get_logger(), "Subscribing to odom topic: %s", odom_topic_.c_str());

    // Create service clients
    head_ai_position_client_ = this->create_client<std_srvs::srv::Trigger>("/mars/head/set_ai_position");
    reload_skills_client_ = this->create_client<std_srvs::srv::Trigger>("/brain/reload_primitives");

    // Create service servers
    new_physical_primitive_srv_ = this->create_service<brain_messages::srv::ManipulationTask>(
        "brain/recorder/new_physical_primitive",
        std::bind(&RecorderNode::handle_new_physical_primitive, this, std::placeholders::_1, std::placeholders::_2));
    
    new_episode_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/new_episode",
        std::bind(&RecorderNode::handle_new_episode, this, std::placeholders::_1, std::placeholders::_2));
    
    save_episode_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/save_episode",
        std::bind(&RecorderNode::handle_save_episode, this, std::placeholders::_1, std::placeholders::_2));
    
    cancel_episode_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/cancel_episode",
        std::bind(&RecorderNode::handle_cancel_episode, this, std::placeholders::_1, std::placeholders::_2));
    
    stop_episode_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/stop_episode",
        std::bind(&RecorderNode::handle_stop_episode, this, std::placeholders::_1, std::placeholders::_2));
    
    end_task_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/end_task",
        std::bind(&RecorderNode::handle_end_task, this, std::placeholders::_1, std::placeholders::_2));
    
    get_task_metadata_list_srv_ = this->create_service<brain_messages::srv::GetTaskMetadataList>(
        "brain/recorder/get_task_metadata_list",
        std::bind(&RecorderNode::handle_get_task_metadata_list, this, std::placeholders::_1, std::placeholders::_2));
    
    update_task_metadata_srv_ = this->create_service<brain_messages::srv::UpdateTaskMetadata>(
        "brain/recorder/update_task_metadata",
        std::bind(&RecorderNode::handle_update_task_metadata, this, std::placeholders::_1, std::placeholders::_2));
    
    get_task_metadata_srv_ = this->create_service<brain_messages::srv::GetTaskMetadata>(
        "brain/recorder/get_task_metadata",
        std::bind(&RecorderNode::handle_get_task_metadata, this, std::placeholders::_1, std::placeholders::_2));

    RCLCPP_INFO(this->get_logger(), "Hosting services:");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/new_physical_primitive");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/new_episode");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/save_episode");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/cancel_episode");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/stop_episode");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/end_task");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/get_task_metadata_list");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/update_task_metadata");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/get_task_metadata");

    // Create status publisher
    status_pub_ = this->create_publisher<brain_messages::msg::RecorderStatus>("/brain/recorder/status", 10);

    // Create recording timer
    auto timer_period = std::chrono::duration<double>(1.0 / data_frequency_);
    timer_ = this->create_wall_timer(timer_period, std::bind(&RecorderNode::timer_callback, this));

    // ========== REPLAY FUNCTIONALITY ==========
    // Use best_effort QoS to match WebRTC streamer's subscriber QoS
    rclcpp::QoS replay_qos(10);
    replay_qos.best_effort();
    replay_main_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
        "/brain/recorder/replay/main_camera/left/image_raw", replay_qos);
    replay_arm_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
        "/brain/recorder/replay/arm_camera/image_raw", replay_qos);
    replay_status_pub_ = this->create_publisher<brain_messages::msg::ReplayStatus>(
        "/brain/recorder/replay_status", 10);

    load_episode_srv_ = this->create_service<brain_messages::srv::LoadEpisode>(
        "brain/recorder/load_episode",
        std::bind(&RecorderNode::handle_load_episode, this, std::placeholders::_1, std::placeholders::_2));
    
    play_replay_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/play_replay",
        std::bind(&RecorderNode::handle_play_replay, this, std::placeholders::_1, std::placeholders::_2));
    
    pause_replay_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/pause_replay",
        std::bind(&RecorderNode::handle_pause_replay, this, std::placeholders::_1, std::placeholders::_2));
    
    stop_replay_srv_ = this->create_service<std_srvs::srv::Trigger>(
        "brain/recorder/stop_replay",
        std::bind(&RecorderNode::handle_stop_replay, this, std::placeholders::_1, std::placeholders::_2));

    RCLCPP_INFO(this->get_logger(), "Replay services initialized:");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/load_episode");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/play_replay");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/pause_replay");
    RCLCPP_INFO(this->get_logger(), "  brain/recorder/stop_replay");

    RCLCPP_INFO(this->get_logger(), "Recorder Node initialized in IDLE state.");
}

void RecorderNode::check_all_topics_received() {
    if (!all_topics_received_) {
        bool all_received = true;
        for (const auto& [topic, received] : topics_received_) {
            if (!received) {
                all_received = false;
                break;
            }
        }
        if (all_received) {
            all_topics_received_ = true;
            RCLCPP_INFO(this->get_logger(), "All topics have received at least one message.");
        }
    }
}

void RecorderNode::image_callback(const sensor_msgs::msg::Image::SharedPtr msg, const std::string& topic) {
    latest_images_[topic] = msg;
    if (!topics_received_[topic]) {
        topics_received_[topic] = true;
        RCLCPP_INFO(this->get_logger(), "First message received on image topic: %s", topic.c_str());
        check_all_topics_received();
    }
}

void RecorderNode::arm_state_callback(const sensor_msgs::msg::JointState::SharedPtr msg) {
    latest_arm_state_ = msg;
    if (!topics_received_[arm_state_topic_]) {
        topics_received_[arm_state_topic_] = true;
        RCLCPP_INFO(this->get_logger(), "First message received on arm state topic: %s", arm_state_topic_.c_str());
        check_all_topics_received();
    }
}

void RecorderNode::leader_command_callback(const std_msgs::msg::Float64MultiArray::SharedPtr msg) {
    latest_leader_command_ = msg;
    if (!topics_received_[leader_command_topic_]) {
        topics_received_[leader_command_topic_] = true;
        RCLCPP_INFO(this->get_logger(), "First message received on leader command topic: %s", leader_command_topic_.c_str());
        check_all_topics_received();
    }
}

void RecorderNode::cmd_vel_callback(const geometry_msgs::msg::Twist::SharedPtr msg) {
    latest_cmd_vel_ = msg;
    if (!topics_received_[velocity_topic_]) {
        topics_received_[velocity_topic_] = true;
        RCLCPP_INFO(this->get_logger(), "First message received on velocity topic: %s", velocity_topic_.c_str());
        check_all_topics_received();
    }
}

void RecorderNode::odom_callback(const nav_msgs::msg::Odometry::SharedPtr msg) {
    latest_odom_ = msg;
}

void RecorderNode::timer_callback() {
    if (state_ != State::EPISODE_ACTIVE || !current_episode_) {
        return;
    }

    // Check for required sensor data
    for (const auto& topic : image_topics_) {
        if (!latest_images_[topic]) {
            RCLCPP_WARN(this->get_logger(), "Incomplete sensor data; missing image from topic %s. Skipping timestep.", topic.c_str());
            return;
        }
    }

    if (!latest_arm_state_) {
        RCLCPP_WARN(this->get_logger(), "Incomplete sensor data; missing arm state. Skipping timestep.");
        return;
    }

    if (!latest_odom_) {
        RCLCPP_WARN(this->get_logger(), "Incomplete sensor data; missing odom. Skipping timestep.");
        return;
    }

    // Build action data
    std::vector<double> action_data;
    if (latest_leader_command_) {
        action_data = std::vector<double>(latest_leader_command_->data.begin(), latest_leader_command_->data.end());
    } else {
        action_data.resize(10, 0.0);
    }

    // Add forward speed and yaw rate
    if (latest_cmd_vel_) {
        action_data.push_back(latest_cmd_vel_->linear.x);
        action_data.push_back(latest_cmd_vel_->angular.z);
    } else {
        action_data.push_back(0.0);
        action_data.push_back(0.0);
    }

    // Get joint positions and velocities
    std::vector<double> qpos(latest_arm_state_->position.begin(), latest_arm_state_->position.end());
    std::vector<double> qvel(latest_arm_state_->velocity.begin(), latest_arm_state_->velocity.end());

    // Get arm timestamp
    double arm_timestamp = latest_arm_state_->header.stamp.sec + 
                           latest_arm_state_->header.stamp.nanosec * 1e-9;

    // Convert images
    std::vector<cv::Mat> images_converted;
    std::vector<double> image_timestamps;
    
    for (const auto& topic : image_topics_) {
        try {
            auto cv_ptr = cv_bridge::toCvCopy(latest_images_[topic], "bgr8");
            
            // Only resize if dimensions don't match
            if (cv_ptr->image.cols != image_size_[0] || cv_ptr->image.rows != image_size_[1]) {
                cv::Mat resized;
                // Use INTER_AREA to match training preprocessing (webdataset.py uses INTER_AREA)
                cv::resize(cv_ptr->image, resized, cv::Size(image_size_[0], image_size_[1]), cv::INTER_AREA);
                images_converted.push_back(resized);
            } else {
                // Already the correct size, use it directly
                images_converted.push_back(cv_ptr->image);
            }

            double img_ts = latest_images_[topic]->header.stamp.sec +
                            latest_images_[topic]->header.stamp.nanosec * 1e-9;
            image_timestamps.push_back(img_ts);
        } catch (const cv_bridge::Exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error converting image from topic %s: %s", topic.c_str(), e.what());
            return;
        }
    }

    try {
        current_episode_->add_timestep(action_data, qpos, qvel, images_converted, arm_timestamp, image_timestamps);
        size_t timestep_count = current_episode_->get_episode_length();
        
        // Check if timestep limit reached
        if (timestep_count >= static_cast<size_t>(max_timesteps_)) {
            RCLCPP_INFO(this->get_logger(), "Timestep limit (%d) reached. Automatically stopping episode.", max_timesteps_);
            state_ = State::EPISODE_STOPPED;
            publish_status("stopped", std::to_string(episode_count_), current_task_name_);
            return;
        }
        
        if (timestep_count % 20 == 0) {
            auto now = std::chrono::steady_clock::now();
            double elapsed = std::chrono::duration<double>(now - episode_start_time_).count();
            RCLCPP_INFO(this->get_logger(), "Recording... %zu timesteps (%.1fs)", timestep_count, elapsed);
        }
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error adding timestep: %s", e.what());
    }
}

void RecorderNode::set_head_ai_position() {
    try {
        if (!head_ai_position_client_->service_is_ready()) {
            RCLCPP_WARN(this->get_logger(), "Head AI position service not available");
            return;
        }

        auto request = std::make_shared<std_srvs::srv::Trigger::Request>();
        head_ai_position_client_->async_send_request(request);
        RCLCPP_INFO(this->get_logger(), "Head AI position command sent");
        std::this_thread::sleep_for(std::chrono::seconds(3));
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error setting AI position: %s", e.what());
    }
}

std::string RecorderNode::state_to_string(State state) {
    switch (state) {
        case State::IDLE: return "IDLE";
        case State::TASK_ACTIVE: return "TASK_ACTIVE";
        case State::EPISODE_ACTIVE: return "EPISODE_ACTIVE";
        case State::EPISODE_STOPPED: return "EPISODE_STOPPED";
        default: return "UNKNOWN";
    }
}

std::string RecorderNode::replay_state_to_string(ReplayState state) {
    switch (state) {
        case ReplayState::IDLE: return "idle";
        case ReplayState::READY: return "ready";
        case ReplayState::PLAYING: return "playing";
        case ReplayState::PAUSED: return "paused";
        case ReplayState::FINISHED: return "finished";
        default: return "unknown";
    }
}

void RecorderNode::publish_status(const std::string& status, const std::string& episode_number,
                                   const std::string& task_name) {
    auto msg = brain_messages::msg::RecorderStatus();
    msg.current_task_name = task_name.empty() ? current_task_name_ : task_name;
    msg.episode_number = episode_number;
    msg.status = status;
    status_pub_->publish(msg);
}

std::string get_timestamp_string() {
    auto now = std::chrono::system_clock::now();
    auto time_t_now = std::chrono::system_clock::to_time_t(now);
    std::tm tm_now;
    localtime_r(&time_t_now, &tm_now);
    std::ostringstream oss;
    oss << std::put_time(&tm_now, "%Y-%m-%dT%H:%M:%S");
    return oss.str();
}

void RecorderNode::handle_new_physical_primitive(
    const std::shared_ptr<brain_messages::srv::ManipulationTask::Request> request,
    std::shared_ptr<brain_messages::srv::ManipulationTask::Response> response) {
    
    if (state_ == State::EPISODE_ACTIVE || state_ == State::EPISODE_STOPPED) {
        RCLCPP_WARN(this->get_logger(), "New physical primitive requested during an %s episode; canceling current episode.",
                    state_to_string(state_).c_str());
        publish_status("cancelled", std::to_string(episode_count_), current_task_name_);
        if (current_episode_) {
            current_episode_->clear();
        }
        current_episode_.reset();
    }

    RCLCPP_INFO(this->get_logger(), "Setting head to AI position for new physical primitive setup");
    set_head_ai_position();

    // Use task_directory if provided, otherwise fall back to default behavior
    if (!request->task_directory.empty()) {
        // Expand ~ to home directory if present
        std::string task_dir = request->task_directory;
        if (!task_dir.empty() && task_dir[0] == '~') {
            const char* home = std::getenv("HOME");
            if (home) {
                task_dir = std::string(home) + task_dir.substr(1);
            }
        }
        RCLCPP_INFO(this->get_logger(), "Using specified task directory: %s", task_dir.c_str());
        task_manager_->start_new_task_at_directory(request->task_name, task_dir, data_frequency_, "learned");
    } else {
        task_manager_->start_new_task(request->task_name, data_frequency_, "learned");
    }
    
    if (task_manager_->has_metadata()) {
        episode_count_ = task_manager_->get_number_of_episodes();
    } else {
        episode_count_ = 0;
    }
    
    current_task_name_ = request->task_name;
    state_ = State::TASK_ACTIVE;
    RCLCPP_INFO(this->get_logger(), "New physical primitive '%s' (type: learned) started.", request->task_name.c_str());
    publish_status("active", "", current_task_name_);
    response->success = true;

    // Trigger skills reload so new skill appears in skills_action_server immediately
    if (reload_skills_client_->service_is_ready()) {
        auto reload_request = std::make_shared<std_srvs::srv::Trigger::Request>();
        reload_skills_client_->async_send_request(reload_request,
            [this](rclcpp::Client<std_srvs::srv::Trigger>::SharedFuture future) {
                auto result = future.get();
                if (result->success) {
                    RCLCPP_INFO(this->get_logger(), "Skills reloaded after new primitive creation.");
                } else {
                    RCLCPP_WARN(this->get_logger(), "Failed to reload skills: %s", result->message.c_str());
                }
            });
    } else {
        RCLCPP_WARN(this->get_logger(), "Reload skills service not available; new skill may not appear until manual reload.");
    }
}

void RecorderNode::handle_new_episode(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (state_ == State::IDLE) {
        RCLCPP_ERROR(this->get_logger(), "Cannot start a new episode unless a task is active.");
        publish_status("failed - no active task", "", "");
        response->success = false;
        response->message = "No active task. Please start a task first.";
        return;
    }
    
    if (state_ == State::EPISODE_ACTIVE || state_ == State::EPISODE_STOPPED) {
        RCLCPP_ERROR(this->get_logger(), "Cannot start a new episode while an episode is %s.", 
                     state_to_string(state_).c_str());
        publish_status("failed - episode " + state_to_string(state_), std::to_string(episode_count_), current_task_name_);
        response->success = false;
        response->message = "An episode is already " + state_to_string(state_) + ". Please save or cancel the current episode first.";
        return;
    }

    current_episode_ = std::make_unique<EpisodeData>();
    episode_start_time_ = std::chrono::steady_clock::now();
    episode_start_system_time_ = std::chrono::system_clock::now();
    state_ = State::EPISODE_ACTIVE;
    episode_count_++;
    
    std::string episode_str = std::to_string(episode_count_);
    RCLCPP_INFO(this->get_logger(), "=== RECORDING STARTED ===");
    RCLCPP_INFO(this->get_logger(), "Task: %s, Episode: %s", current_task_name_.c_str(), episode_str.c_str());
    RCLCPP_INFO(this->get_logger(), "Recording at %d Hz", data_frequency_);
    
    publish_status("active", episode_str, current_task_name_);
    response->success = true;
    response->message = "Episode started.";
}

void RecorderNode::handle_save_episode(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if ((state_ != State::EPISODE_ACTIVE && state_ != State::EPISODE_STOPPED) || !current_episode_) {
        RCLCPP_ERROR(this->get_logger(), "No active or stopped episode to save.");
        if (state_ == State::TASK_ACTIVE) {
            publish_status("failed - no active/stopped episode to save", "", current_task_name_);
        } else {
            publish_status("failed - no active task", "", "");
        }
        response->success = false;
        response->message = "No active or stopped episode to save.";
        return;
    }

    if (current_episode_->get_episode_length() == 0) {
        RCLCPP_ERROR(this->get_logger(), "Cannot save empty episode with no timesteps.");
        publish_status("failed - empty episode", std::to_string(episode_count_), current_task_name_);
        response->success = false;
        response->message = "Cannot save empty episode.";
        return;
    }

    auto end_time = std::chrono::steady_clock::now();
    double duration = std::chrono::duration<double>(end_time - episode_start_time_).count();
    size_t timesteps = current_episode_->get_episode_length();

    // Convert system clock time points to timestamp strings
    auto end_system_time = std::chrono::system_clock::now();
    auto start_time_t = std::chrono::system_clock::to_time_t(episode_start_system_time_);
    auto end_time_t = std::chrono::system_clock::to_time_t(end_system_time);
    std::tm tm_start, tm_end;
    localtime_r(&start_time_t, &tm_start);
    localtime_r(&end_time_t, &tm_end);
    std::ostringstream oss_start, oss_end;
    oss_start << std::put_time(&tm_start, "%Y-%m-%dT%H:%M:%S");
    oss_end << std::put_time(&tm_end, "%Y-%m-%dT%H:%M:%S");
    std::string start_ts = oss_start.str();
    std::string end_ts = oss_end.str();

    task_manager_->add_episode(*current_episode_, start_ts, end_ts);
    
    RCLCPP_INFO(this->get_logger(), "=== EPISODE SAVED ===");
    RCLCPP_INFO(this->get_logger(), "Task: %s, Episode: %d", current_task_name_.c_str(), episode_count_);
    RCLCPP_INFO(this->get_logger(), "Duration: %.1fs, Timesteps: %zu", duration, timesteps);
    
    publish_status("saved", std::to_string(episode_count_), current_task_name_);
    current_episode_.reset();
    state_ = State::TASK_ACTIVE;
    response->success = true;
    response->message = "Episode saved.";
}

void RecorderNode::handle_cancel_episode(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if ((state_ != State::EPISODE_ACTIVE && state_ != State::EPISODE_STOPPED) || !current_episode_) {
        RCLCPP_ERROR(this->get_logger(), "No active or stopped episode to cancel.");
        if (state_ == State::TASK_ACTIVE) {
            publish_status("failed - no active/stopped episode to cancel", "", current_task_name_);
        } else {
            publish_status("failed - no active task", "", "");
        }
        response->success = false;
        response->message = "No active or stopped episode to cancel.";
        return;
    }

    current_episode_->clear();
    if (episode_count_ > 0) {
        episode_count_--;
    }
    RCLCPP_INFO(this->get_logger(), "Episode canceled; buffered data discarded.");
    publish_status("cancelled", std::to_string(episode_count_), current_task_name_);
    current_episode_.reset();
    state_ = State::TASK_ACTIVE;
    response->success = true;
    response->message = "Episode canceled.";
}

void RecorderNode::handle_stop_episode(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (state_ == State::EPISODE_ACTIVE) {
        state_ = State::EPISODE_STOPPED;
        RCLCPP_INFO(this->get_logger(), "Episode recording stopped. Waiting for save or cancel command.");
        publish_status("stopped", std::to_string(episode_count_), current_task_name_);
        response->success = true;
        response->message = "Episode recording stopped. Awaiting save or cancel.";
    } else if (state_ == State::EPISODE_STOPPED) {
        RCLCPP_WARN(this->get_logger(), "Stop episode requested, but episode is already stopped.");
        publish_status("failed - episode already stopped", std::to_string(episode_count_), current_task_name_);
        response->success = false;
        response->message = "Episode is already stopped.";
    } else if (state_ == State::TASK_ACTIVE) {
        RCLCPP_ERROR(this->get_logger(), "Stop episode requested, but no episode is active.");
        publish_status("failed - no active episode", "", current_task_name_);
        response->success = false;
        response->message = "No active episode to stop.";
    } else {
        RCLCPP_ERROR(this->get_logger(), "Stop episode requested, but no task is active.");
        publish_status("failed - no active task", "", "");
        response->success = false;
        response->message = "No active task or episode to stop.";
    }
}

void RecorderNode::handle_end_task(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (state_ == State::EPISODE_ACTIVE || state_ == State::EPISODE_STOPPED) {
        RCLCPP_WARN(this->get_logger(), "Ending task during an %s episode; canceling current episode first.",
                    state_to_string(state_).c_str());
        publish_status("cancelled", std::to_string(episode_count_), current_task_name_);
        if (current_episode_) {
            current_episode_->clear();
        }
        current_episode_.reset();
    }
    
    task_manager_->end_task();
    state_ = State::IDLE;
    RCLCPP_INFO(this->get_logger(), "Task ended; recorder state set to IDLE.");
    publish_status("idle", "", "");
    current_task_name_.clear();
    episode_count_ = 0;
    response->success = true;
    response->message = "Task ended.";

    // Trigger skills reload so new skill appears in skills_action_server
    if (reload_skills_client_->service_is_ready()) {
        auto reload_request = std::make_shared<std_srvs::srv::Trigger::Request>();
        reload_skills_client_->async_send_request(reload_request,
            [this](rclcpp::Client<std_srvs::srv::Trigger>::SharedFuture future) {
                auto result = future.get();
                if (result->success) {
                    RCLCPP_INFO(this->get_logger(), "Skills reloaded successfully.");
                } else {
                    RCLCPP_WARN(this->get_logger(), "Failed to reload skills: %s", result->message.c_str());
                }
            });
    } else {
        RCLCPP_WARN(this->get_logger(), "Reload skills service not available; skills may need manual reload.");
    }
}

void RecorderNode::handle_get_task_metadata_list(
    const std::shared_ptr<brain_messages::srv::GetTaskMetadataList::Request> /*request*/,
    std::shared_ptr<brain_messages::srv::GetTaskMetadataList::Response> response) {
    
    RCLCPP_INFO(this->get_logger(), "Received request to get task metadata list.");
    
    try {
        auto all_tasks_summary = task_manager_->get_all_tasks_summary();
        
        if (all_tasks_summary.empty()) {
            RCLCPP_INFO(this->get_logger(), "No tasks found by TaskManager.");
            response->success = true;
            response->message = "No tasks recorded yet.";
            response->json_metadata = "[]";
            return;
        }

        nlohmann::json json_array = nlohmann::json::array();
        for (const auto& task : all_tasks_summary) {
            json_array.push_back(task);
        }
        
        response->json_metadata = json_array.dump(2);
        response->success = true;
        response->message = "Successfully retrieved task metadata list.";
        RCLCPP_INFO(this->get_logger(), "Successfully prepared task metadata list.");
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Failed to get task metadata list: %s", e.what());
        response->success = false;
        response->message = std::string("Error retrieving task metadata: ") + e.what();
        response->json_metadata = "{}";
    }
}

void RecorderNode::handle_update_task_metadata(
    const std::shared_ptr<brain_messages::srv::UpdateTaskMetadata::Request> request,
    std::shared_ptr<brain_messages::srv::UpdateTaskMetadata::Response> response) {
    
    RCLCPP_INFO(this->get_logger(), "Received request to update metadata for task directory: %s", 
                request->task_directory.c_str());
    
    try {
        auto [success, message] = task_manager_->update_task_metadata_by_directory(
            request->task_directory, request->json_metadata_update);
        response->success = success;
        response->message = message;
        
        if (success) {
            RCLCPP_INFO(this->get_logger(), "Successfully updated metadata for task directory: %s",
                        request->task_directory.c_str());
        } else {
            RCLCPP_ERROR(this->get_logger(), "Failed to update metadata for task directory %s: %s",
                         request->task_directory.c_str(), message.c_str());
        }
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Exception while updating task metadata for directory %s: %s",
                     request->task_directory.c_str(), e.what());
        response->success = false;
        response->message = std::string("Error updating task metadata: ") + e.what();
    }
}

void RecorderNode::handle_get_task_metadata(
    const std::shared_ptr<brain_messages::srv::GetTaskMetadata::Request> request,
    std::shared_ptr<brain_messages::srv::GetTaskMetadata::Response> response) {
    
    RCLCPP_INFO(this->get_logger(), "Received request to get metadata for task directory: %s",
                request->task_directory.c_str());
    
    try {
        auto [success, message, metadata_json] = task_manager_->get_task_metadata_by_directory(request->task_directory);
        response->success = success;
        response->message = message;
        response->json_metadata = metadata_json;

        if (success) {
            RCLCPP_INFO(this->get_logger(), "Successfully retrieved metadata for task directory: %s",
                        request->task_directory.c_str());
        } else {
            RCLCPP_ERROR(this->get_logger(), "Failed to retrieve metadata for task directory %s: %s",
                         request->task_directory.c_str(), message.c_str());
        }
    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Exception while retrieving task metadata for directory %s: %s",
                     request->task_directory.c_str(), e.what());
        response->success = false;
        response->message = std::string("Error retrieving task metadata: ") + e.what();
        response->json_metadata = "{}";
    }
}

// ========== REPLAY FUNCTIONALITY ==========

void RecorderNode::handle_load_episode(
    const std::shared_ptr<brain_messages::srv::LoadEpisode::Request> request,
    std::shared_ptr<brain_messages::srv::LoadEpisode::Response> response) {
    
    RCLCPP_INFO(this->get_logger(), "Loading episode %d from %s", 
                request->episode_id, request->task_directory.c_str());
    
    try {
        // Stop any current replay
        if (replay_state_ == ReplayState::PLAYING || replay_state_ == ReplayState::PAUSED) {
            stop_replay_timer();
            replay_state_ = ReplayState::IDLE;
        }

        std::string data_dir = request->task_directory + "/data";
        std::string h5_path = data_dir + "/episode_" + std::to_string(request->episode_id) + ".h5";

        if (!fs::exists(h5_path)) {
            response->success = false;
            response->message = "Episode file not found: " + h5_path;
            response->num_frames = 0;
            response->fps = 0.0;
            response->duration_sec = 0.0;
            response->arm_data_json = "{}";
            return;
        }

        // Load metadata for fps
        std::string metadata_path = data_dir + "/dataset_metadata.json";
        if (fs::exists(metadata_path)) {
            std::ifstream f(metadata_path);
            nlohmann::json metadata;
            f >> metadata;
            replay_fps_ = metadata.value("data_frequency", 10.0);
        } else {
            replay_fps_ = static_cast<double>(data_frequency_);
        }

        // Load images from HDF5
        hid_t file_id = H5Fopen(h5_path.c_str(), H5F_ACC_RDONLY, H5P_DEFAULT);
        if (file_id < 0) {
            response->success = false;
            response->message = "Failed to open HDF5 file: " + h5_path;
            response->num_frames = 0;
            response->fps = 0.0;
            response->duration_sec = 0.0;
            response->arm_data_json = "{}";
            return;
        }

        // Check for images group
        if (H5Lexists(file_id, "/observations/images", H5P_DEFAULT) <= 0) {
            H5Fclose(file_id);
            response->success = false;
            response->message = "No images found in episode file";
            response->num_frames = 0;
            response->fps = 0.0;
            response->duration_sec = 0.0;
            response->arm_data_json = "{}";
            return;
        }

        hid_t img_group = H5Gopen2(file_id, "/observations/images", H5P_DEFAULT);
        
        // Get camera names
        std::vector<std::string> camera_names;
        hsize_t num_objs;
        H5Gget_num_objs(img_group, &num_objs);
        for (hsize_t i = 0; i < num_objs; i++) {
            char name[256];
            H5Gget_objname_by_idx(img_group, i, name, sizeof(name));
            camera_names.push_back(std::string(name));
        }

        if (camera_names.empty()) {
            H5Gclose(img_group);
            H5Fclose(file_id);
            response->success = false;
            response->message = "No camera data found in episode";
            response->num_frames = 0;
            response->fps = 0.0;
            response->duration_sec = 0.0;
            response->arm_data_json = "{}";
            return;
        }

        // Load images into buffer
        replay_buffer_.clear();
        for (const auto& cam_name : camera_names) {
            hid_t dataset = H5Dopen2(img_group, cam_name.c_str(), H5P_DEFAULT);
            hid_t dataspace = H5Dget_space(dataset);
            
            int ndims = H5Sget_simple_extent_ndims(dataspace);
            std::vector<hsize_t> dims(ndims);
            H5Sget_simple_extent_dims(dataspace, dims.data(), nullptr);

            // dims: [num_frames, height, width, channels]
            size_t num_frames = dims[0];
            int height = static_cast<int>(dims[1]);
            int width = static_cast<int>(dims[2]);
            int channels = static_cast<int>(dims[3]);

            std::vector<uint8_t> flat_data(num_frames * height * width * channels);
            H5Dread(dataset, H5T_NATIVE_UINT8, H5S_ALL, H5S_ALL, H5P_DEFAULT, flat_data.data());

            // Convert to cv::Mat vector
            std::vector<cv::Mat> frames;
            frames.reserve(num_frames);
            for (size_t f = 0; f < num_frames; f++) {
                cv::Mat frame(height, width, CV_8UC3, flat_data.data() + f * height * width * channels);
                frames.push_back(frame.clone());
            }
            replay_buffer_[cam_name] = std::move(frames);
            
            RCLCPP_INFO(this->get_logger(), "Loaded %s: [%zu, %d, %d, %d]", 
                        cam_name.c_str(), num_frames, height, width, channels);

            H5Sclose(dataspace);
            H5Dclose(dataset);
        }
        H5Gclose(img_group);

        // Get frame count
        replay_total_frames_ = static_cast<int>(replay_buffer_.begin()->second.size());

        // Load arm data and timestamps
        nlohmann::json arm_data;
        arm_data["arm_timestamps"] = nlohmann::json::array();
        arm_data["image_timestamps"] = nlohmann::json::object();
        arm_data["qpos"] = nlohmann::json::array();
        arm_data["qvel"] = nlohmann::json::array();

        // Load arm timestamps
        if (H5Lexists(file_id, "/timestamps/arm", H5P_DEFAULT) > 0) {
            hid_t ts_dataset = H5Dopen2(file_id, "/timestamps/arm", H5P_DEFAULT);
            hid_t ts_space = H5Dget_space(ts_dataset);
            hsize_t ts_dims[1];
            H5Sget_simple_extent_dims(ts_space, ts_dims, nullptr);
            
            std::vector<double> arm_ts(ts_dims[0]);
            H5Dread(ts_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, arm_ts.data());
            
            if (!arm_ts.empty()) {
                double first_ts = arm_ts[0];
                for (auto& ts : arm_ts) {
                    arm_data["arm_timestamps"].push_back(ts - first_ts);
                }
            }
            RCLCPP_INFO(this->get_logger(), "Loaded arm timestamps: %zu frames", arm_ts.size());
            
            H5Sclose(ts_space);
            H5Dclose(ts_dataset);
        } else {
            // Fallback: compute timestamps from fps
            for (int i = 0; i < replay_total_frames_; i++) {
                arm_data["arm_timestamps"].push_back(static_cast<double>(i) / replay_fps_);
            }
            RCLCPP_INFO(this->get_logger(), "No timestamps in H5, using computed timestamps");
        }

        // Load qpos
        if (H5Lexists(file_id, "/observations/qpos", H5P_DEFAULT) > 0) {
            hid_t qpos_dataset = H5Dopen2(file_id, "/observations/qpos", H5P_DEFAULT);
            hid_t qpos_space = H5Dget_space(qpos_dataset);
            hsize_t qpos_dims[2];
            H5Sget_simple_extent_dims(qpos_space, qpos_dims, nullptr);
            
            std::vector<double> qpos_data(qpos_dims[0] * qpos_dims[1]);
            H5Dread(qpos_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, qpos_data.data());
            
            for (size_t i = 0; i < qpos_dims[0]; i++) {
                nlohmann::json row = nlohmann::json::array();
                for (size_t j = 0; j < qpos_dims[1]; j++) {
                    row.push_back(qpos_data[i * qpos_dims[1] + j]);
                }
                arm_data["qpos"].push_back(row);
            }
            RCLCPP_INFO(this->get_logger(), "Loaded qpos: [%llu, %llu]", qpos_dims[0], qpos_dims[1]);
            
            H5Sclose(qpos_space);
            H5Dclose(qpos_dataset);
        }

        // Load qvel
        if (H5Lexists(file_id, "/observations/qvel", H5P_DEFAULT) > 0) {
            hid_t qvel_dataset = H5Dopen2(file_id, "/observations/qvel", H5P_DEFAULT);
            hid_t qvel_space = H5Dget_space(qvel_dataset);
            hsize_t qvel_dims[2];
            H5Sget_simple_extent_dims(qvel_space, qvel_dims, nullptr);
            
            std::vector<double> qvel_data(qvel_dims[0] * qvel_dims[1]);
            H5Dread(qvel_dataset, H5T_NATIVE_DOUBLE, H5S_ALL, H5S_ALL, H5P_DEFAULT, qvel_data.data());
            
            for (size_t i = 0; i < qvel_dims[0]; i++) {
                nlohmann::json row = nlohmann::json::array();
                for (size_t j = 0; j < qvel_dims[1]; j++) {
                    row.push_back(qvel_data[i * qvel_dims[1] + j]);
                }
                arm_data["qvel"].push_back(row);
            }
            RCLCPP_INFO(this->get_logger(), "Loaded qvel: [%llu, %llu]", qvel_dims[0], qvel_dims[1]);
            
            H5Sclose(qvel_space);
            H5Dclose(qvel_dataset);
        }

        H5Fclose(file_id);

        // Set replay metadata
        replay_frame_index_ = 0;
        replay_task_name_ = fs::path(request->task_directory).filename().string();
        replay_episode_id_ = "episode_" + std::to_string(request->episode_id);
        replay_state_ = ReplayState::READY;

        double duration = static_cast<double>(replay_total_frames_) / replay_fps_;
        RCLCPP_INFO(this->get_logger(), "Episode loaded: %d frames, %.1f fps, %.1fs", 
                    replay_total_frames_, replay_fps_, duration);

        publish_replay_status();

        response->success = true;
        response->message = "Episode loaded successfully";
        response->num_frames = replay_total_frames_;
        response->fps = static_cast<float>(replay_fps_);
        response->duration_sec = static_cast<float>(duration);
        response->arm_data_json = arm_data.dump();

    } catch (const std::exception& e) {
        RCLCPP_ERROR(this->get_logger(), "Error loading episode: %s", e.what());
        response->success = false;
        response->message = std::string("Error loading episode: ") + e.what();
        response->num_frames = 0;
        response->fps = 0.0;
        response->duration_sec = 0.0;
        response->arm_data_json = "{}";
    }
}

void RecorderNode::handle_play_replay(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (replay_state_ == ReplayState::IDLE) {
        response->success = false;
        response->message = "No episode loaded. Call load_episode first.";
        return;
    }

    if (replay_state_ == ReplayState::PLAYING) {
        response->success = false;
        response->message = "Replay is already playing.";
        return;
    }

    if (replay_state_ == ReplayState::FINISHED) {
        replay_frame_index_ = 0;
    }

    start_replay_timer();
    replay_state_ = ReplayState::PLAYING;
    
    RCLCPP_INFO(this->get_logger(), "Replay started from frame %d", replay_frame_index_);
    publish_replay_status();
    
    response->success = true;
    response->message = "Replay started from frame " + std::to_string(replay_frame_index_);
}

void RecorderNode::handle_pause_replay(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (replay_state_ != ReplayState::PLAYING) {
        response->success = false;
        response->message = "Cannot pause: replay is " + replay_state_to_string(replay_state_);
        return;
    }

    stop_replay_timer();
    replay_state_ = ReplayState::PAUSED;
    
    RCLCPP_INFO(this->get_logger(), "Replay paused at frame %d", replay_frame_index_);
    publish_replay_status();
    
    response->success = true;
    response->message = "Replay paused at frame " + std::to_string(replay_frame_index_);
}

void RecorderNode::handle_stop_replay(
    const std::shared_ptr<std_srvs::srv::Trigger::Request> /*request*/,
    std::shared_ptr<std_srvs::srv::Trigger::Response> response) {
    
    if (replay_state_ == ReplayState::IDLE) {
        response->success = false;
        response->message = "No episode loaded.";
        return;
    }

    stop_replay_timer();
    replay_frame_index_ = 0;
    replay_state_ = ReplayState::READY;
    
    RCLCPP_INFO(this->get_logger(), "Replay stopped and reset to frame 0");
    publish_replay_status();
    
    response->success = true;
    response->message = "Replay stopped and reset to frame 0";
}

void RecorderNode::start_replay_timer() {
    if (replay_timer_) {
        replay_timer_->cancel();
    }
    auto period = std::chrono::duration<double>(1.0 / replay_fps_);
    replay_timer_ = this->create_wall_timer(period, std::bind(&RecorderNode::replay_timer_callback, this));
}

void RecorderNode::stop_replay_timer() {
    if (replay_timer_) {
        replay_timer_->cancel();
        replay_timer_.reset();
    }
}

void RecorderNode::replay_timer_callback() {
    if (replay_state_ != ReplayState::PLAYING) {
        return;
    }

    if (replay_frame_index_ >= replay_total_frames_) {
        stop_replay_timer();
        replay_state_ = ReplayState::FINISHED;
        RCLCPP_INFO(this->get_logger(), "Replay finished");
        publish_replay_status();
        return;
    }

    int idx = 0;
    for (const auto& [cam_name, frames] : replay_buffer_) {
        const cv::Mat& frame = frames[replay_frame_index_];

        // Convert cv::Mat to Image message using cv_bridge
        try {
            cv_bridge::CvImage cv_image;
            cv_image.image = frame;
            cv_image.encoding = "bgr8";
            cv_image.header.stamp = this->get_clock()->now();
            cv_image.header.frame_id = cam_name;

            auto msg = cv_image.toImageMsg();

            // Publish to appropriate topic
            if (idx == 0 || cam_name.find("main") != std::string::npos || cam_name == "camera_1") {
                replay_main_pub_->publish(*msg);
            } else {
                replay_arm_pub_->publish(*msg);
            }
        } catch (const cv_bridge::Exception& e) {
            RCLCPP_WARN(this->get_logger(), "Failed to convert frame for %s: %s", cam_name.c_str(), e.what());
            continue;
        }
        idx++;
    }

    replay_frame_index_++;

    if (replay_frame_index_ % 10 == 0) {
        publish_replay_status();
    }
}

void RecorderNode::publish_replay_status() {
    auto msg = brain_messages::msg::ReplayStatus();
    msg.state = replay_state_to_string(replay_state_);
    msg.current_frame = replay_frame_index_;
    msg.total_frames = replay_total_frames_;
    msg.current_time_sec = replay_fps_ > 0 ? static_cast<float>(replay_frame_index_) / replay_fps_ : 0.0f;
    msg.total_time_sec = replay_fps_ > 0 ? static_cast<float>(replay_total_frames_) / replay_fps_ : 0.0f;
    msg.episode_id = replay_episode_id_;
    msg.task_name = replay_task_name_;
    replay_status_pub_->publish(msg);
}

}  // namespace manipulation
