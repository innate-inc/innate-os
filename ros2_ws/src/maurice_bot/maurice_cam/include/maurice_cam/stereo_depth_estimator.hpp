#pragma once

#include <array>
#include <memory>
#include <string>
#include <vector>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"
#include "stereo_msgs/msg/disparity_image.hpp"

#include "maurice_cam/stereo_calibration.hpp"

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
  // ── VPI Stereo (depth_estimator/vpi_stereo.cpp) ─────────────────────────
  bool initializeVPI();
  void cleanupVPI();
  bool submitSGM(const cv::Mat& left_rect, const cv::Mat& right_rect);
  void syncSGM();
  cv::Mat extractDisparity();
  void cleanupSGMWraps();

  // ── Rectification (depth_estimator/rectification.cpp) ──────────────────
  void scaleToCalibRes(const cv::Mat& left_in, const cv::Mat& right_in,
                       cv::Mat& left_out, cv::Mat& right_out);
  void rectifyMono(const cv::Mat& left_scaled, const cv::Mat& right_scaled,
                   cv::Mat& left_rect, cv::Mat& right_rect);
  void rectifyColor(const cv::Mat& left_scaled, cv::Mat& left_color_rect);
#ifdef USE_VPI_REMAP
  bool initVPIRemap();
  void cleanupVPIRemap();
#endif

  // ── Publishing (depth_estimator/publishing.cpp) ────────────────────────
  void publishMonoRectified(const cv::Mat& left_rect, const cv::Mat& right_rect,
                            const rclcpp::Time& ts, bool pub_left, bool pub_right);
  void publishColorRectified(const cv::Mat& color_rect, const cv::Mat& mono_rect,
                             bool has_color_input, const rclcpp::Time& ts,
                             bool pub_color, bool pub_compressed);
  void publishDisparityMsg(const cv::Mat& disparity_float, const rclcpp::Time& ts,
                           rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr& pub);
  void publishDepth(const cv::Mat& disparity_float, const rclcpp::Time& ts);

  // ── Point Cloud (depth_estimator/pointcloud.cpp) ───────────────────────
  void publishPointCloudXYZ(const cv::Mat& disparity_lowres, const rclcpp::Time& ts);
  void publishPointCloudColor(const cv::Mat& disparity_lowres, const cv::Mat& color_rect,
                              const rclcpp::Time& ts);

  // ── Callback / Pipeline ────────────────────────────────────────────────
  void syncCallback(
    const sensor_msgs::msg::Image::ConstSharedPtr& left_msg,
    const sensor_msgs::msg::Image::ConstSharedPtr& right_msg);
  void processFrame(const cv::Mat& left_img, const cv::Mat& right_img,
                    const rclcpp::Time& timestamp);

  // ── Disparity Filter Chain (filters/*.cpp) ──────────────────────────
  struct FilterTimings {
    double downsample_ms{0}, upsample_ms{0};
    double depth_clamp_ms{0}, domain_transform_ms{0}, speckle_ms{0};
    double edge_inv_ms{0}, median_ms{0}, bilateral_ms{0};
    double hole_fill_ms{0}, temporal_ms{0};
  };
  void initFilterParams();      // filters/filter_chain.cpp
  void logFilterConfig() const; // filters/filter_chain.cpp
  void applyFilterChain(cv::Mat& disparity, cv::Mat& disparity_lowres,
                        FilterTimings& timings, float focal_length, float baseline);
  // Individual filters (filters/simple_filters.cpp, filters/advanced_filters.cpp)
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
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr left_rectified_color_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr left_rectified_compressed_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr pointcloud_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr pointcloud_color_pub_;

  // ── General Parameters ─────────────────────────────────────────────────
  std::string data_directory_;
  std::string left_topic_, right_topic_;
  std::string depth_topic_, disparity_topic_, disparity_unfiltered_topic_;
  std::string left_rectified_topic_, right_rectified_topic_;
  std::string left_rectified_color_topic_;
  std::string left_rectified_compressed_topic_;
  std::string pointcloud_topic_;
  std::string pointcloud_color_topic_;
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

  // Calibration (shared utility class)
  std::shared_ptr<StereoCalibration> stereo_calib_;
  double baseline_, focal_length_;
  int jpeg_quality_{80};

  // Rectification maps (calibration resolution)
  cv::Mat map1_left_, map2_left_;
  cv::Mat map1_right_, map2_right_;

  // VPI resources
  VPIStream vpi_stream_{nullptr};
  VPIImage vpi_disparity_{nullptr};
  VPIImage vpi_confidence_{nullptr};
  VPIPayload stereo_payload_{nullptr};

  // Per-frame VPI image wraps (created in submitSGM, destroyed in cleanupSGMWraps)
  VPIImage vpi_left_wrap_{nullptr};
  VPIImage vpi_right_wrap_{nullptr};

#ifdef USE_VPI_REMAP
  // VPI remap resources (persistent across frames)
  VPIPayload vpi_remap_left_{nullptr};
  VPIPayload vpi_remap_right_{nullptr};
  VPIPayload vpi_remap_color_{nullptr};
  VPIImage   vpi_rect_left_out_{nullptr};
  VPIImage   vpi_rect_right_out_{nullptr};
  VPIImage   vpi_rect_color_out_{nullptr};
  bool       vpi_remap_ready_{false};
#endif

  bool vpi_initialized_{false};
  bool calibration_loaded_{false};
  int frame_count_{0};
  int input_frame_count_{0};
  rclcpp::Time last_stats_time_;

  // ── Filter Parameters ──────────────────────────────────────────────────
  bool filter_downsample_enabled_{false};
  int filter_downsample_factor_{4};
  bool median_enabled_{false};
  int median_kernel_size_{5};
  bool bilateral_enabled_{false};
  int bilateral_diameter_{5};
  double bilateral_sigma_color_{10.0}, bilateral_sigma_space_{10.0};
  bool hole_filling_enabled_{false};
  int hole_fill_radius_{2};
  int hole_fill_strategy_{1};
  bool depth_clamp_enabled_{false};
  double min_depth_meters_{0.25}, max_depth_meters_{5.0};
  bool edge_inv_enabled_{false};
  int edge_inv_width_{3};
  double edge_canny_low_{10.0}, edge_canny_high_{30.0};
  bool speckle_enabled_{false};
  int speckle_max_size_{200};
  double speckle_max_diff_{1.0};
  bool dt_enabled_{false};
  int dt_iterations_{2}, dt_delta_{20};
  double dt_alpha_{0.5};
  bool temporal_enabled_{false};
  double temporal_alpha_{0.4};
  int temporal_delta_{20};
  int temporal_persistence_{3};
  cv::Mat prev_disparity_frame_;
  std::vector<uint8_t> temporal_history_;
  std::vector<std::string> filter_order_;


};

} // namespace maurice_cam
