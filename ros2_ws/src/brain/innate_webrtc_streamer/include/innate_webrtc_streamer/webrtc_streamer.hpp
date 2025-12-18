#ifndef INNATE_WEBRTC_STREAMER__WEBRTC_STREAMER_HPP_
#define INNATE_WEBRTC_STREAMER__WEBRTC_STREAMER_HPP_

#define GST_USE_UNSTABLE_API

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>

#include <gst/gst.h>
#include <gst/webrtc/webrtc.h>
#include <gst/sdp/sdp.h>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

#include <string>
#include <memory>
#include <mutex>

namespace innate_webrtc_streamer
{

class WebRTCStreamer : public rclcpp::Node
{
public:
  WebRTCStreamer();
  ~WebRTCStreamer();

private:
  // ROS2 callbacks
  void on_start(const std_msgs::msg::String::SharedPtr msg);
  void on_answer(const std_msgs::msg::String::SharedPtr msg);
  void on_ice_in(const std_msgs::msg::String::SharedPtr msg);
  void on_image_main(const sensor_msgs::msg::CompressedImage::SharedPtr msg);
  void on_image_arm(const sensor_msgs::msg::CompressedImage::SharedPtr msg);

  // GStreamer callbacks (static for C callback interface)
  static void on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data);
  static void on_connection_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
  static void on_ice_gathering_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
  static void on_offer_created(GstPromise* promise, gpointer user_data);

  // Helper methods
  void create_subscriptions(const std::string& source);
  void destroy_subscriptions();
  void cleanup_pipeline();
  cv::Mat process_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg, int target_width, int target_height);
  void push_frame(GstElement* appsrc, const cv::Mat& frame);

  // Publishers
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr offer_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ice_out_pub_;

  // Subscribers
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr answer_sub_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr ice_in_sub_;
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr start_sub_;
  rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_main_;
  rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_arm_;

  // GStreamer elements (initialized first in constructor)
  GstElement* pipeline_;
  GstElement* webrtc_;
  GstElement* appsrc_main_;
  GstElement* appsrc_arm_;

  // Source mode (initialized early)
  std::string current_source_;

  // QoS profile for cameras
  rclcpp::QoS camera_qos_;

  // Topic configuration
  std::string live_main_topic_;
  std::string live_arm_topic_;
  std::string replay_main_topic_;
  std::string replay_arm_topic_;

  // Thread safety
  std::mutex pipeline_mutex_;
};

}  // namespace innate_webrtc_streamer

#endif  // INNATE_WEBRTC_STREAMER__WEBRTC_STREAMER_HPP_
