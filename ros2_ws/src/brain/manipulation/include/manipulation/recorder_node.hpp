#ifndef MANIPULATION_RECORDER_NODE_HPP_
#define MANIPULATION_RECORDER_NODE_HPP_

#include <memory>
#include <string>
#include <vector>
#include <map>
#include <chrono>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <geometry_msgs/msg/twist.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_srvs/srv/trigger.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>

#include "brain_messages/msg/recorder_status.hpp"
#include "brain_messages/msg/replay_status.hpp"
#include "brain_messages/srv/manipulation_task.hpp"
#include "brain_messages/srv/get_task_metadata.hpp"
#include "brain_messages/srv/get_task_metadata_list.hpp"
#include "brain_messages/srv/load_episode.hpp"
#include "brain_messages/srv/update_task_metadata.hpp"

#include "manipulation/episode_data.hpp"
#include "manipulation/task_manager.hpp"

namespace manipulation {

class RecorderNode : public rclcpp::Node {
public:
    RecorderNode();
    ~RecorderNode() override = default;

private:
    // State enum
    enum class State {
        IDLE,
        TASK_ACTIVE,
        EPISODE_ACTIVE,
        EPISODE_STOPPED
    };

    // Replay state enum
    enum class ReplayState {
        IDLE,
        READY,
        PLAYING,
        PAUSED,
        FINISHED
    };

    // Callbacks
    void image_callback(const sensor_msgs::msg::Image::SharedPtr msg, const std::string& topic);
    void arm_state_callback(const sensor_msgs::msg::JointState::SharedPtr msg);
    void leader_command_callback(const std_msgs::msg::Float64MultiArray::SharedPtr msg);
    void cmd_vel_callback(const geometry_msgs::msg::Twist::SharedPtr msg);
    void odom_callback(const nav_msgs::msg::Odometry::SharedPtr msg);
    void timer_callback();

    // Service handlers
    void handle_new_physical_primitive(
        const std::shared_ptr<brain_messages::srv::ManipulationTask::Request> request,
        std::shared_ptr<brain_messages::srv::ManipulationTask::Response> response);
    void handle_new_episode(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_save_episode(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_cancel_episode(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_stop_episode(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_end_task(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_get_task_metadata_list(
        const std::shared_ptr<brain_messages::srv::GetTaskMetadataList::Request> request,
        std::shared_ptr<brain_messages::srv::GetTaskMetadataList::Response> response);
    void handle_update_task_metadata(
        const std::shared_ptr<brain_messages::srv::UpdateTaskMetadata::Request> request,
        std::shared_ptr<brain_messages::srv::UpdateTaskMetadata::Response> response);
    void handle_get_task_metadata(
        const std::shared_ptr<brain_messages::srv::GetTaskMetadata::Request> request,
        std::shared_ptr<brain_messages::srv::GetTaskMetadata::Response> response);

    // Replay service handlers
    void handle_load_episode(
        const std::shared_ptr<brain_messages::srv::LoadEpisode::Request> request,
        std::shared_ptr<brain_messages::srv::LoadEpisode::Response> response);
    void handle_play_replay(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_pause_replay(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);
    void handle_stop_replay(
        const std::shared_ptr<std_srvs::srv::Trigger::Request> request,
        std::shared_ptr<std_srvs::srv::Trigger::Response> response);

    // Helper methods
    void check_all_topics_received();
    void publish_status(const std::string& status, const std::string& episode_number = "", 
                        const std::string& current_task_name = "");
    void set_head_ai_position();
    std::string state_to_string(State state);
    std::string replay_state_to_string(ReplayState state);

    // Replay helpers
    void start_replay_timer();
    void stop_replay_timer();
    void replay_timer_callback();
    void publish_replay_status();

    // Parameters
    std::string data_directory_;
    int data_frequency_;
    std::vector<std::string> image_topics_;
    std::string arm_state_topic_;
    std::string leader_command_topic_;
    std::string velocity_topic_;
    std::string odom_topic_;
    std::vector<int64_t> image_size_;
    int max_timesteps_;

    // Task management
    std::unique_ptr<TaskManager> task_manager_;
    std::unique_ptr<EpisodeData> current_episode_;
    State state_;
    std::chrono::steady_clock::time_point episode_start_time_;
    std::chrono::system_clock::time_point episode_start_system_time_;
    std::string current_task_name_;
    int episode_count_;

    // Sensor data
    std::map<std::string, sensor_msgs::msg::Image::SharedPtr> latest_images_;
    sensor_msgs::msg::JointState::SharedPtr latest_arm_state_;
    std_msgs::msg::Float64MultiArray::SharedPtr latest_leader_command_;
    geometry_msgs::msg::Twist::SharedPtr latest_cmd_vel_;
    nav_msgs::msg::Odometry::SharedPtr latest_odom_;

    // Topic tracking
    std::map<std::string, bool> topics_received_;
    bool all_topics_received_;

    // Subscribers
    std::vector<rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr> image_subs_;
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr arm_state_sub_;
    rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr leader_command_sub_;
    rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr cmd_vel_sub_;
    rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odom_sub_;

    // Publishers
    rclcpp::Publisher<brain_messages::msg::RecorderStatus>::SharedPtr status_pub_;

    // Services
    rclcpp::Service<brain_messages::srv::ManipulationTask>::SharedPtr new_physical_primitive_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr new_episode_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr save_episode_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr cancel_episode_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr stop_episode_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr end_task_srv_;
    rclcpp::Service<brain_messages::srv::GetTaskMetadataList>::SharedPtr get_task_metadata_list_srv_;
    rclcpp::Service<brain_messages::srv::UpdateTaskMetadata>::SharedPtr update_task_metadata_srv_;
    rclcpp::Service<brain_messages::srv::GetTaskMetadata>::SharedPtr get_task_metadata_srv_;

    // Service clients
    rclcpp::Client<std_srvs::srv::Trigger>::SharedPtr head_ai_position_client_;
    rclcpp::Client<std_srvs::srv::Trigger>::SharedPtr reload_skills_client_;

    // Timer
    rclcpp::TimerBase::SharedPtr timer_;

    // ========== REPLAY FUNCTIONALITY ==========
    ReplayState replay_state_;
    std::map<std::string, std::vector<cv::Mat>> replay_buffer_;
    int replay_frame_index_;
    int replay_total_frames_;
    double replay_fps_;
    std::string replay_task_name_;
    std::string replay_episode_id_;
    rclcpp::TimerBase::SharedPtr replay_timer_;

    // Replay publishers
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr replay_main_pub_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr replay_arm_pub_;
    rclcpp::Publisher<brain_messages::msg::ReplayStatus>::SharedPtr replay_status_pub_;

    // Replay services
    rclcpp::Service<brain_messages::srv::LoadEpisode>::SharedPtr load_episode_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr play_replay_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr pause_replay_srv_;
    rclcpp::Service<std_srvs::srv::Trigger>::SharedPtr stop_replay_srv_;
};

}  // namespace manipulation

#endif  // MANIPULATION_RECORDER_NODE_HPP_
