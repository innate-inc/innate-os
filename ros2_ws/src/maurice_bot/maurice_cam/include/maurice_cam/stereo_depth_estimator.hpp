#pragma once

#include <memory>
#include <string>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"

#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>
#include <geometry_msgs/msg/transform_stamped.hpp>

#include <opencv2/opencv.hpp>

// VPI headers
#include <vpi/VPI.h>
#include <vpi/OpenCVInterop.hpp>
#include <vpi/algo/StereoDisparity.h>

namespace maurice_cam
{

/**
 * @brief Stereo Depth Estimator component node using NVIDIA VPI
 * 
 * This node subscribes to stereo image topic using zero-copy intra-process
 * communication, performs GPU-accelerated stereo matching using VPI Block
 * Matching algorithm, and publishes the depth image.
 * 
 * Key features:
 * - Scales input to calibration resolution for processing
 * - Uses VPI Block Matching (faster than SGM, good quality)
 * - No post-filtering (cleanest results per testing)
 * - Scales depth back to input resolution for publishing
 */
class StereoDepthEstimator : public rclcpp::Node
{
public:
  /**
   * @brief Constructor
   * @param options Node options for component composition
   */
  explicit StereoDepthEstimator(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());

  /**
   * @brief Destructor
   */
  ~StereoDepthEstimator();

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
   * @brief Initialize VPI resources (streams, payloads, images)
   * @return true if successful, false otherwise
   */
  bool initializeVPI();

  /**
   * @brief Cleanup VPI resources
   */
  void cleanupVPI();

  /**
   * @brief Callback for stereo image subscription
   * @param msg Incoming stereo image message (zero-copy unique_ptr)
   */
  void stereoImageCallback(sensor_msgs::msg::Image::UniquePtr msg);

  /**
   * @brief Process stereo frame: rectify, compute disparity, compute depth
   * @param stereo_frame Input stereo frame (side-by-side left and right)
   * @param timestamp Timestamp for the output message
   */
  void processFrame(const cv::Mat& stereo_frame, const rclcpp::Time& timestamp);

  // ROS 2 publishers and subscribers
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr stereo_sub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr depth_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr disparity_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr rectified_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr pointcloud_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr filtered_pointcloud_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr camera_info_pub_;

  // TF2 for transforming points to base_link
  std::shared_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;

  // Node parameters
  std::string data_directory_;
  std::string stereo_topic_;
  std::string depth_topic_;
  std::string disparity_topic_;
  std::string rectified_topic_;
  std::string pointcloud_topic_;
  std::string filtered_pointcloud_topic_;
  std::string camera_info_topic_;
  std::string frame_id_;
  std::string base_frame_id_;  // base_link frame for filtering
  int max_disparity_;
  bool publish_disparity_;
  bool publish_rectified_;
  bool publish_pointcloud_;
  bool publish_filtered_pointcloud_;
  int process_every_n_frames_;  // Process 1 out of every N frames
  int pointcloud_decimation_;   // Decimate point cloud (1=full, 2=half, 4=quarter)

  // Filtered pointcloud parameters (relative to base_link)
  // base_link frame: Z=up (height), X=forward, Y=left
  double filter_max_obstacle_height_;  // Max Z in base_link (height above ground)
  double filter_min_obstacle_height_;  // Min Z in base_link
  double filter_max_range_;            // Max horizontal range (XY distance)
  double filter_min_range_;            // Min horizontal range

  // Block Matching parameters (from Python script that worked best)
  int bm_window_size_;          // Block matching window size (default: 11)
  int bm_quality_;              // Quality level 0-8 (default: 8 = max quality)
  int bm_conf_threshold_;       // Confidence threshold (default: 16000, lower = more details)

  // Image dimensions - input (from camera)
  int stereo_width_;   // Full stereo image width (left + right) from camera
  int stereo_height_;  // Full stereo image height from camera
  int image_width_;    // Single camera image width (stereo_width / 2)
  int image_height_;   // Single camera image height

  // Image dimensions - calibration (processing resolution)
  int calib_width_;    // Calibration image width (from YAML)
  int calib_height_;   // Calibration image height (from YAML)
  double depth_scale_; // Scale factor: calib_width / image_width

  // Calibration parameters (loaded from YAML)
  cv::Mat K1_, D1_;  // Left camera intrinsics and distortion
  cv::Mat K2_, D2_;  // Right camera intrinsics and distortion
  cv::Mat R_, T_;    // Rotation and translation between cameras
  cv::Mat R1_, R2_;  // Rectification transforms
  cv::Mat P1_, P2_;  // Projection matrices
  cv::Mat Q_;        // Disparity-to-depth mapping matrix
  double baseline_;  // Baseline distance (meters)
  double focal_length_; // Focal length in pixels (after rectification)

  // OpenCV rectification maps (at calibration resolution)
  cv::Mat map1_left_, map2_left_;
  cv::Mat map1_right_, map2_right_;

  // VPI resources
  VPIStream vpi_stream_{nullptr};
  
  // VPI images (at calibration resolution)
  VPIImage vpi_disparity_{nullptr};
  VPIImage vpi_confidence_{nullptr};

  // VPI stereo payload
  VPIPayload stereo_payload_{nullptr};

  // Processing state
  bool vpi_initialized_{false};
  bool calibration_loaded_{false};

  // Frame statistics and rate control
  int frame_count_{0};
  int input_frame_count_{0};  // Total input frames received
  rclcpp::Time last_stats_time_;
};

} // namespace maurice_cam
