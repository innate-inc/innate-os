#pragma once

#include <memory>
#include <string>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"
#include "stereo_msgs/msg/disparity_image.hpp"

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
 * @brief Stereo depth estimator with integrated disparity filtering and
 *        point cloud generation.
 *
 * Pipeline (single node, zero-copy):
 *   left/right raw → VPI SGM (CUDA) → disparity filtering → depth + point cloud
 *
 * Filter chain (each stage independently toggleable):
 *   Median → Bilateral → Hole Fill → Depth Clamp → Edge Invalidation
 *   → Speckle Removal → Domain Transform → Temporal Smoothing
 */
class StereoDepthEstimator : public rclcpp::Node
{
public:
  explicit StereoDepthEstimator(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());
  ~StereoDepthEstimator();

private:
  // ── Setup ──────────────────────────────────────────────────────────────
  std::filesystem::path findCalibrationConfigDir();
  bool loadCalibration(const std::filesystem::path& calib_path);
  bool initializeVPI();
  void cleanupVPI();

  // ── Processing ─────────────────────────────────────────────────────────
  void syncCallback(
    const sensor_msgs::msg::Image::ConstSharedPtr& left_msg,
    const sensor_msgs::msg::Image::ConstSharedPtr& right_msg);
  void processFrame(const cv::Mat& left_img, const cv::Mat& right_img,
                    const rclcpp::Time& timestamp);
  void publishDisparityMsg(const cv::Mat& disparity_float,
                           const rclcpp::Time& timestamp,
                           rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr& pub);

  // ── Disparity Filters ──────────────────────────────────────────────────
  void initFilterParams();
  void logFilterConfig();
  void applyFilterChain(cv::Mat& disparity, cv::Mat& disparity_lowres);
  void applyMedian(cv::Mat& img);
  void applyBilateral(cv::Mat& img);
  void fillHoles(cv::Mat& img);
  void clampByDepth(cv::Mat& img, float f, float t);
  void invalidateEdges(cv::Mat& img);
  void filterSpeckles(cv::Mat& img);
  void applyDomainTransform(cv::Mat& img);
  void dtPass(cv::Mat& img, bool horizontal);
  void applyTemporal(cv::Mat& img);

  // ── Sync ───────────────────────────────────────────────────────────────
  using SyncPolicy = message_filters::sync_policies::ApproximateTime<
    sensor_msgs::msg::Image, sensor_msgs::msg::Image>;
  using Synchronizer = message_filters::Synchronizer<SyncPolicy>;

  std::shared_ptr<message_filters::Subscriber<sensor_msgs::msg::Image>> left_sub_;
  std::shared_ptr<message_filters::Subscriber<sensor_msgs::msg::Image>> right_sub_;
  std::shared_ptr<Synchronizer> sync_;

  // ── Publishers ─────────────────────────────────────────────────────────
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr depth_pub_;
  rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr disparity_pub_;
  rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr disparity_unfiltered_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr left_rectified_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr right_rectified_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr pointcloud_pub_;

  // ── General Parameters ─────────────────────────────────────────────────
  std::string data_directory_;
  std::string left_topic_, right_topic_;
  std::string depth_topic_, disparity_topic_, disparity_unfiltered_topic_;
  std::string left_rectified_topic_, right_rectified_topic_;
  std::string pointcloud_topic_;
  std::string frame_id_;
  int max_disparity_;
  int process_every_n_frames_;
  int pointcloud_decimation_;

  // VPI SGM parameters
  int include_diagonals_;
  int confidence_threshold_;
  int min_disparity_;
  int p1_, p2_;
  double uniqueness_;
  int disparity_border_margin_;

  // Image dimensions
  int image_width_, image_height_;       // input (from camera)
  int calib_width_, calib_height_;       // calibration (processing)
  double depth_scale_;

  // Calibration matrices
  cv::Mat K1_, D1_, K2_, D2_;
  cv::Mat R_, T_;
  cv::Mat R1_, R2_, P1_, P2_, Q_;
  double baseline_, focal_length_;

  // Rectification maps (calibration resolution)
  cv::Mat map1_left_, map2_left_;
  cv::Mat map1_right_, map2_right_;

  // VPI resources
  VPIStream vpi_stream_{nullptr};
  VPIImage vpi_disparity_{nullptr};
  VPIImage vpi_confidence_{nullptr};
  VPIPayload stereo_payload_{nullptr};

  bool vpi_initialized_{false};
  bool calibration_loaded_{false};
  int frame_count_{0};
  int input_frame_count_{0};
  rclcpp::Time last_stats_time_;

  // ── Filter Parameters ──────────────────────────────────────────────────
  // 0. Downsample
  bool filter_downsample_enabled_;
  int filter_downsample_factor_;
  // 1. Median
  bool median_enabled_;
  int median_kernel_size_;
  // 2. Bilateral
  bool bilateral_enabled_;
  int bilateral_diameter_;
  double bilateral_sigma_color_, bilateral_sigma_space_;
  // 3. Hole filling
  bool hole_filling_enabled_;
  int hole_fill_radius_;
  int hole_fill_strategy_;  // 0=fill_from_left, 1=farthest, 2=nearest
  // 4. Depth clamping
  bool depth_clamp_enabled_;
  double min_depth_meters_, max_depth_meters_;
  // 5. Edge invalidation
  bool edge_inv_enabled_;
  int edge_inv_width_;
  double edge_canny_low_, edge_canny_high_;
  // 6. Speckle
  bool speckle_enabled_;
  int speckle_max_size_;
  double speckle_max_diff_;
  // 7. Domain transform
  bool dt_enabled_;
  int dt_iterations_, dt_delta_;
  double dt_alpha_;
  // 8. Temporal
  bool temporal_enabled_;
  double temporal_alpha_;
  int temporal_delta_;                     // edge-preserving threshold (disparity units)
  int temporal_persistence_;               // 0=disabled, 1..8 = RS persistence modes
  cv::Mat prev_disparity_frame_;
  std::vector<uint8_t> temporal_history_;  // per-pixel 8-frame validity ring
  // Filter execution order
  std::vector<std::string> filter_order_;
};

} // namespace maurice_cam
