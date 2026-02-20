#pragma once

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <opencv2/opencv.hpp>
#include <thread>
#include <atomic>
#include <mutex>

namespace maurice_cam
{

/**
 * @brief GStreamer-based arm camera driver node for Maurice robot
 * 
 * This node uses GStreamer with MJPG format to capture 1080p frames from the 
 * arm-mounted Arducam USB camera. It publishes:
 *   - Downscaled 640x480 raw + compressed on the standard topics
 *   - Full 1080p raw + compressed on high-res topics
 * 
 * GStreamer provides robust device handling and hardware-accelerated
 * color conversion on Jetson platforms.
 */
class ArmCameraDriver : public rclcpp::Node
{
public:
  /**
   * @brief Constructor
   * @param options Node options for component composition
   */
  explicit ArmCameraDriver(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());

  /**
   * @brief Destructor
   */
  ~ArmCameraDriver();

private:
  /**
   * @brief Initialize camera with GStreamer pipeline
   * @return true if successful, false otherwise
   */
  bool initializeCamera();

  /**
   * @brief Create GStreamer pipeline string for YUYV capture
   * @return GStreamer pipeline string
   */
  std::string createGStreamerPipeline();

  /**
   * @brief Frame processing loop (runs in separate thread)
   */
  void frameProcessingLoop();

  /**
   * @brief Process and publish a captured frame at both resolutions
   * @param frame The captured 1080p frame
   */
  void processAndPublishFrame(const cv::Mat& frame);

  // ROS 2 publishers — standard (downscaled)
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;

  // ROS 2 publishers — high-res (native 1080p)
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr hires_image_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr hires_compressed_pub_;

  // OpenCV VideoCapture with GStreamer backend
  cv::VideoCapture cap_;
  mutable std::mutex cap_mutex_;  // Protects cap_ access across threads

  // Frame processing thread
  std::thread frame_thread_;
  std::atomic<bool> frame_thread_running_{false};

  // Camera parameters (capture at native high-res)
  std::string device_path_;
  int capture_width_;
  int capture_height_;
  int publish_width_;
  int publish_height_;
  double fps_;

  // Compressed image publishing settings
  bool publish_compressed_{false};
  int compressed_frame_interval_{5};
  int compressed_frame_counter_{0};
  int hires_compressed_frame_interval_{1};
  int hires_compressed_frame_counter_{0};
};

} // namespace maurice_cam
