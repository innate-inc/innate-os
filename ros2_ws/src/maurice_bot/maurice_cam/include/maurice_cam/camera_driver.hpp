#pragma once

#include <chrono>
#include <memory>
#include <string>
#include <thread>
#include <atomic>
#include <deque>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <cstring>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "cv_bridge/cv_bridge.h"

#include <opencv2/opencv.hpp>

namespace maurice_cam
{

/**
 * @brief PID-based auto exposure controller with center weighting
 */
class AutoExposureController
{
public:
  AutoExposureController();
  
  /**
   * @brief Initialize the controller with exposure limits and PID parameters
   * @param min_exposure Minimum exposure value
   * @param max_exposure Maximum exposure value
   * @param target_brightness Target brightness (0-255)
   * @param kp Proportional gain
   * @param ki Integral gain
   * @param kd Derivative gain
   */
  void initialize(int min_exposure, int max_exposure, double target_brightness = 128.0,
                 double kp = 0.5, double ki = 0.1, double kd = 0.05);
  
  /**
   * @brief Calculate new exposure value based on frame brightness
   * @param frame Input frame
   * @param current_exposure Current exposure value
   * @return New exposure value
   */
  int calculateExposure(const cv::Mat& frame, int current_exposure);
  
  /**
   * @brief Reset PID controller state
   */
  void reset();
  
  /**
   * @brief Update PID parameters
   */
  void setPID(double kp, double ki, double kd);
  
  /**
   * @brief Update target brightness
   */
  void setTargetBrightness(double target);

private:
  /**
   * @brief Calculate center-weighted brightness of the frame
   * @param frame Input frame
   * @return Center-weighted brightness (0-255)
   */
  double calculateCenterWeightedBrightness(const cv::Mat& frame);
  
  // PID controller parameters
  double kp_, ki_, kd_;
  double target_brightness_;
  
  // PID controller state
  double last_error_;
  double integral_;
  
  // Exposure limits
  int min_exposure_, max_exposure_;
  
  // Center weighting parameters
  double center_weight_;  // Weight for center region (0.0-1.0)
  double center_region_size_;  // Size of center region (0.0-1.0)
};

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
   * @brief Initialize V4L2 controls for manual exposure/gain control
   * @return true if successful, false otherwise
   */
  bool initializeV4L2Controls();

  /**
   * @brief Set a V4L2 control value
   * @param control_id V4L2 control ID
   * @param value Value to set
   * @return true if successful, false otherwise
   */
  bool setV4L2Control(int control_id, int value);

  /**
   * @brief Get a V4L2 control value
   * @param control_id V4L2 control ID
   * @return Control value, or -1 if failed
   */
  int getV4L2Control(int control_id);

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

  /**
   * @brief Apply auto exposure control to frame
   * @param frame Input frame for brightness analysis
   */
  void applyAutoExposure(const cv::Mat& frame);

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

  // V4L2 control interface
  int camera_fd_{-1};
  
  // Camera control parameters
  int exposure_min_, exposure_max_;
  int gain_min_, gain_max_;
  int current_exposure_;
  int current_gain_;
  bool v4l2_controls_initialized_{false};
  int exposure_setting_{-1};
  int gain_setting_{-1};
  bool disable_auto_exposure_{false};
  int default_gain_param_{110};

  // Auto exposure control
  AutoExposureController auto_exposure_controller_;
  bool enable_auto_exposure_{true};
  double target_brightness_{128.0};
  double ae_kp_{0.5};
  double ae_ki_{0.1};
  double ae_kd_{0.05};
  int auto_exposure_update_interval_{5};  // Update every N frames
  int frame_counter_{0};

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
