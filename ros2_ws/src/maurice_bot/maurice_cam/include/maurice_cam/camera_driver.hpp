#pragma once

#include <chrono>
#include <memory>
#include <string>
#include <thread>
#include <atomic>
#include <deque>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "cv_bridge/cv_bridge.h"

#include <opencv2/opencv.hpp>

namespace maurice_cam
{

/**
 * @brief GStreamer-based camera driver node for Maurice robot
 * 
 * This node provides a clean interface to capture frames from a USB camera
 * using GStreamer pipeline and publishes both raw and compressed images.
 */
class CameraDriver : public rclcpp::Node
{
public:
  /**
   * @brief Constructor
   */
  CameraDriver();

  /**
   * @brief Destructor
   */
  ~CameraDriver();

private:
  /**
   * @brief Initialize camera with GStreamer pipeline
   * @return true if successful, false otherwise
   */
  bool initializeCamera();

  /**
   * @brief Create GStreamer pipeline string
   * @return Pipeline string for camera capture
   */
  std::string createGStreamerPipeline();

  /**
   * @brief Main frame processing loop
   */
  void frameProcessingLoop();

  /**
   * @brief Update frame statistics
   */
  void updateFrameStats();

  /**
   * @brief Print frame statistics
   */
  void printFrameStats();

  /**
   * @brief Process captured frame and publish messages
   * @param frame Captured frame from camera
   */
  void processAndPublishFrame(const cv::Mat& frame);

  // ROS 2 publishers
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr stereo_pub_;

  // Frame processing thread
  std::thread frame_thread_;
  std::atomic<bool> frame_thread_running_{false};

  // Camera parameters
  std::string camera_device_;
  int capture_width_;
  int capture_height_;
  int left_width_;
  int left_height_;
  double fps_;
  std::string frame_id_;
  int jpeg_quality_;

  // OpenCV camera capture
  cv::VideoCapture cap_;

  // Frame timing tracking
  std::deque<rclcpp::Time> frame_timestamps_;
  rclcpp::Time last_stats_print_;
  
  // Statistics
  std::atomic<int> frame_count_{0};
  std::atomic<bool> camera_initialized_{false};
};

} // namespace maurice_cam
