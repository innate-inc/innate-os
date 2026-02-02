#pragma once

#include <memory>
#include <string>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/srv/set_camera_info.hpp"

#include <opencv2/opencv.hpp>

namespace maurice_cam
{

/**
 * @brief Main Camera Info publisher node
 * 
 * This node:
 * - Loads stereo calibration from YAML file
 * - Subscribes to left and right image topics
 * - Publishes CameraInfo synced to each image timestamp
 * - Provides services to update calibration (with backup)
 * 
 * Topics subscribed:
 * - /mars/main_camera/left/image_raw  (sensor_msgs/Image)
 * - /mars/main_camera/right/image_raw (sensor_msgs/Image)
 * 
 * Topics published:
 * - /mars/main_camera/left/camera_info  (sensor_msgs/CameraInfo)
 * - /mars/main_camera/right/camera_info (sensor_msgs/CameraInfo)
 * 
 * Services:
 * - /mars/main_camera/left/set_camera_info  (sensor_msgs/SetCameraInfo)
 * - /mars/main_camera/right/set_camera_info (sensor_msgs/SetCameraInfo)
 */
class MainCameraInfo : public rclcpp::Node
{
public:
  /**
   * @brief Constructor
   * @param options Node options for component composition
   */
  explicit MainCameraInfo(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());

  /**
   * @brief Destructor
   */
  ~MainCameraInfo();

private:
  /**
   * @brief Find calibration config directory based on robot_info.json
   * @return Path to calibration config directory
   */
  std::filesystem::path findCalibrationConfigDir();

  /**
   * @brief Load stereo calibration parameters from YAML file
   * @param calib_path Path to the calibration YAML file
   * @return true if successful, false otherwise
   */
  bool loadCalibration(const std::filesystem::path& calib_path);

  /**
   * @brief Build CameraInfo message from calibration matrices
   * @param K Intrinsic matrix (3x3)
   * @param D Distortion coefficients (1x5)
   * @param R Rectification matrix (3x3)
   * @param P Projection matrix (3x4)
   * @param frame_id Frame ID for the camera
   * @param negate_tx If true, negate P[0,3] to convert from OpenCV to ROS convention
   * @return Populated CameraInfo message
   */
  sensor_msgs::msg::CameraInfo buildCameraInfo(
    const cv::Mat& K, const cv::Mat& D, 
    const cv::Mat& R, const cv::Mat& P,
    const std::string& frame_id,
    bool negate_tx = false);

  /**
   * @brief Left image callback - publishes left camera_info synced to image
   * @param msg Incoming image message
   */
  void leftImageCallback(const sensor_msgs::msg::Image::ConstSharedPtr& msg);

  /**
   * @brief Right image callback - publishes right camera_info synced to image
   * @param msg Incoming image message
   */
  void rightImageCallback(const sensor_msgs::msg::Image::ConstSharedPtr& msg);

  /**
   * @brief Service callback to set left camera calibration
   * @param request Service request containing CameraInfo
   * @param response Service response with success/failure status
   */
  void handleSetLeftCameraInfo(
    const std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Request> request,
    std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Response> response);

  /**
   * @brief Service callback to set right camera calibration
   * @param request Service request containing CameraInfo
   * @param response Service response with success/failure status
   */
  void handleSetRightCameraInfo(
    const std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Request> request,
    std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Response> response);

  /**
   * @brief Save calibration to YAML file (backing up existing)
   * @param left_info Left camera info (or nullptr to keep existing)
   * @param right_info Right camera info (or nullptr to keep existing)
   * @return true if successful, false otherwise
   */
  bool saveCalibration(
    const sensor_msgs::msg::CameraInfo* left_info,
    const sensor_msgs::msg::CameraInfo* right_info);

  /**
   * @brief Backup an existing calibration file
   * @param calib_path Path to the calibration file
   * @return Path to the backup file, or empty string if no backup was needed
   */
  std::string backupCalibration(const std::filesystem::path& calib_path);

  // ROS 2 publishers, subscribers, and services
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr left_info_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr right_info_pub_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr left_image_sub_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr right_image_sub_;
  rclcpp::Service<sensor_msgs::srv::SetCameraInfo>::SharedPtr set_left_info_srv_;
  rclcpp::Service<sensor_msgs::srv::SetCameraInfo>::SharedPtr set_right_info_srv_;

  // Node parameters
  std::string data_directory_;
  std::string left_image_topic_;
  std::string right_image_topic_;
  std::string left_info_topic_;
  std::string right_info_topic_;
  std::string left_frame_id_;
  std::string right_frame_id_;

  // Image dimensions (from calibration)
  int image_width_;
  int image_height_;

  // Calibration parameters (loaded from YAML)
  cv::Mat K1_, D1_;  // Left camera intrinsics and distortion
  cv::Mat K2_, D2_;  // Right camera intrinsics and distortion
  cv::Mat R_, T_;    // Rotation and translation between cameras
  cv::Mat R1_, R2_;  // Rectification transforms
  cv::Mat P1_, P2_;  // Projection matrices
  cv::Mat Q_;        // Disparity-to-depth mapping matrix

  // Calibration file path (for updates)
  std::filesystem::path calib_file_path_;

  // Pre-built camera info messages
  sensor_msgs::msg::CameraInfo left_info_msg_;
  sensor_msgs::msg::CameraInfo right_info_msg_;

  // State
  bool calibration_loaded_{false};
};

} // namespace maurice_cam
