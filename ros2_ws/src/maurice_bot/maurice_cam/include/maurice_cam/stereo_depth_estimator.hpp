#pragma once

#include <memory>
#include <string>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"

#include <message_filters/subscriber.h>
#include <message_filters/sync_policies/approximate_time.h>
#include <message_filters/synchronizer.h>

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
 * This node subscribes to separate left and right image topics using
 * message_filters for time synchronization, performs GPU-accelerated stereo
 * matching using VPI Block Matching algorithm, and publishes the depth image.
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
   * @brief Callback for synchronized left/right image subscription
   * @param left_msg Left camera image
   * @param right_msg Right camera image
   */
  void syncCallback(
    const sensor_msgs::msg::Image::ConstSharedPtr& left_msg,
    const sensor_msgs::msg::Image::ConstSharedPtr& right_msg);

  /**
   * @brief Process left and right images: rectify, compute disparity, compute depth
   * @param left_img Input left image
   * @param right_img Input right image
   * @param timestamp Timestamp for the output message
   */
  void processFrame(const cv::Mat& left_img, const cv::Mat& right_img, const rclcpp::Time& timestamp);

  // ROS 2 publishers and subscribers
  using SyncPolicy = message_filters::sync_policies::ApproximateTime<
    sensor_msgs::msg::Image, sensor_msgs::msg::Image>;
  using Synchronizer = message_filters::Synchronizer<SyncPolicy>;
  
  std::shared_ptr<message_filters::Subscriber<sensor_msgs::msg::Image>> left_sub_;
  std::shared_ptr<message_filters::Subscriber<sensor_msgs::msg::Image>> right_sub_;
  std::shared_ptr<Synchronizer> sync_;
  
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr depth_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr disparity_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr left_rectified_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr right_rectified_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr pointcloud_pub_;

  // Node parameters
  std::string data_directory_;
  std::string left_topic_;
  std::string right_topic_;
  std::string depth_topic_;
  std::string disparity_topic_;
  std::string left_rectified_topic_;
  std::string right_rectified_topic_;
  std::string pointcloud_topic_;
  std::string frame_id_;
  int max_disparity_;
  int process_every_n_frames_;  // Process 1 out of every N frames
  int pointcloud_decimation_;   // Decimate point cloud (1=full, 2=half, 4=quarter)

  // VPI SGM creation parameters (set at payload creation)
  int include_diagonals_;       // Include diagonal paths in SGM (0=no, 1=yes)
  
  // VPI SGM runtime parameters (CUDA backend)
  int confidence_threshold_;    // Confidence threshold (0-65535)
  int min_disparity_;           // Minimum disparity (0 to max_disparity)
  int p1_;                      // Penalty for +/-1 disparity change
  int p2_;                      // Penalty for >1 disparity change (must be < 256)
  double uniqueness_;           // Uniqueness ratio (-1 to disable, 0-1)
  int disparity_border_margin_; // Zero out N pixels from border of disparity

  // Image dimensions - input (from camera)
  int image_width_;    // Single camera image width
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
