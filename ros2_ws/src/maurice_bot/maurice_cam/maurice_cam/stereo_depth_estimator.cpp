// Stereo Depth Estimator — orchestrator.
//
// All heavy lifting lives in sibling files under depth_estimator/ and filters/:
//   depth_estimator/vpi_stereo.cpp    — VPI init/cleanup, submit, sync, extract
//   depth_estimator/rectification.cpp — scale, mono/colour rectification
//   depth_estimator/publishing.cpp    — disparity, depth, rectified image publishing
//   depth_estimator/pointcloud.cpp    — xyz and xyzrgb point cloud generation
//   filters/filter_chain.cpp          — filter chain init, log, orchestration
//   filters/simple_filters.cpp        — median, bilateral, hole-fill, clamp, edge, speckle
//   filters/advanced_filters.cpp      — domain-transform, temporal
//
// This file contains:  constructor, destructor, syncCallback, processFrame, main.

#include "maurice_cam/stereo_depth_estimator.hpp"

using namespace std::chrono_literals;

namespace maurice_cam
{

// =============================================================================
// Constructor — parameter declaration, calibration, publishers, subscribers
// =============================================================================
StereoDepthEstimator::StereoDepthEstimator(const rclcpp::NodeOptions & options)
: Node("stereo_depth_estimator", options)
{
  // Declare parameters with defaults
  this->declare_parameter<std::string>("data_directory", "/home/jetson1/innate-os/data");
  this->declare_parameter<std::string>("left_topic", "/mars/main_camera/left/image_raw");
  this->declare_parameter<std::string>("right_topic", "/mars/main_camera/right/image_raw");
  this->declare_parameter<std::string>("depth_topic", "/mars/main_camera/depth");
  this->declare_parameter<std::string>("disparity_topic", "/mars/main_camera/disparity");
  this->declare_parameter<std::string>("disparity_unfiltered_topic", "/mars/main_camera/disparity_unfiltered");
  this->declare_parameter<std::string>("left_rectified_topic", "/mars/main_camera/left/image_rect");
  this->declare_parameter<std::string>("right_rectified_topic", "/mars/main_camera/right/image_rect");
  this->declare_parameter<std::string>("left_rectified_color_topic", "/mars/main_camera/left/image_rect_color");
  this->declare_parameter<std::string>("left_rectified_compressed_topic", "/mars/main_camera/left/image_rect_color/compressed");
  this->declare_parameter<std::string>("frame_id", "camera_optical_frame");
  this->declare_parameter<int>("jpeg_quality", 80);
  this->declare_parameter<int>("image_width", 640);
  this->declare_parameter<int>("image_height", 480);
  this->declare_parameter<int>("process_every_n_frames", 1);
  this->declare_parameter<std::string>("pointcloud_topic", "/mars/main_camera/points");
  this->declare_parameter<std::string>("pointcloud_color_topic", "/mars/main_camera/points_color");
  this->declare_parameter<int>("pointcloud_decimation", 2);

  // VPI creation parameters
  this->declare_parameter<int>("max_disparity", 64);
  this->declare_parameter<int>("include_diagonals", 1);

  // VPI runtime parameters (CUDA backend)
  this->declare_parameter<int>("confidence_threshold", 32767);
  this->declare_parameter<int>("min_disparity", 0);
  this->declare_parameter<int>("p1", 3);
  this->declare_parameter<int>("p2", 48);
  this->declare_parameter<double>("uniqueness", 0.15);
  this->declare_parameter<int>("disparity_border_margin", 10);

  // Get parameter values
  data_directory_ = this->get_parameter("data_directory").as_string();
  left_topic_ = this->get_parameter("left_topic").as_string();
  right_topic_ = this->get_parameter("right_topic").as_string();
  depth_topic_ = this->get_parameter("depth_topic").as_string();
  disparity_topic_ = this->get_parameter("disparity_topic").as_string();
  disparity_unfiltered_topic_ = this->get_parameter("disparity_unfiltered_topic").as_string();
  left_rectified_topic_ = this->get_parameter("left_rectified_topic").as_string();
  right_rectified_topic_ = this->get_parameter("right_rectified_topic").as_string();
  left_rectified_color_topic_ = this->get_parameter("left_rectified_color_topic").as_string();
  left_rectified_compressed_topic_ = this->get_parameter("left_rectified_compressed_topic").as_string();
  frame_id_ = this->get_parameter("frame_id").as_string();
  jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();
  image_width_ = this->get_parameter("image_width").as_int();
  image_height_ = this->get_parameter("image_height").as_int();
  process_every_n_frames_ = this->get_parameter("process_every_n_frames").as_int();
  if (process_every_n_frames_ < 1) process_every_n_frames_ = 1;
  pointcloud_topic_ = this->get_parameter("pointcloud_topic").as_string();
  pointcloud_color_topic_ = this->get_parameter("pointcloud_color_topic").as_string();
  pointcloud_decimation_ = this->get_parameter("pointcloud_decimation").as_int();
  if (pointcloud_decimation_ < 1) pointcloud_decimation_ = 1;

  max_disparity_ = this->get_parameter("max_disparity").as_int();
  include_diagonals_ = this->get_parameter("include_diagonals").as_int();
  if (max_disparity_ < 1) max_disparity_ = 1;
  if (max_disparity_ > 256) max_disparity_ = 256;

  confidence_threshold_ = this->get_parameter("confidence_threshold").as_int();
  min_disparity_ = this->get_parameter("min_disparity").as_int();
  p1_ = this->get_parameter("p1").as_int();
  p2_ = this->get_parameter("p2").as_int();
  uniqueness_ = this->get_parameter("uniqueness").as_double();
  disparity_border_margin_ = this->get_parameter("disparity_border_margin").as_int();

  if (p2_ >= 256) {
    RCLCPP_WARN(this->get_logger(), "CUDA requires p2 < 256, clamping %d -> 255", p2_);
    p2_ = 255;
  }

  // Initialize disparity filter parameters
  initFilterParams();

  RCLCPP_INFO(this->get_logger(), "=== Maurice Stereo Depth Estimator (VPI SGM CUDA) ===");
  RCLCPP_INFO(this->get_logger(), "Data directory: %s", data_directory_.c_str());
  RCLCPP_INFO(this->get_logger(), "Left topic: %s", left_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Right topic: %s", right_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Depth topic: %s", depth_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Input image dimensions: %dx%d", image_width_, image_height_);
  RCLCPP_INFO(this->get_logger(), "Max disparity: %d", max_disparity_);
  RCLCPP_INFO(this->get_logger(), "SGM params: diagonals=%d, p1=%d, p2=%d, confThreshold=%d, uniqueness=%.2f",
              include_diagonals_, p1_, p2_, confidence_threshold_, uniqueness_);
  if (process_every_n_frames_ > 1) {
    RCLCPP_INFO(this->get_logger(), "Process rate: 1/%d frames", process_every_n_frames_);
  }

  // Load stereo calibration
  try {
    stereo_calib_ = StereoCalibration::load(data_directory_);
    calib_width_  = stereo_calib_->calibWidth();
    calib_height_ = stereo_calib_->calibHeight();
    focal_length_ = stereo_calib_->focalLength();
    baseline_     = stereo_calib_->baseline();
    depth_scale_  = static_cast<double>(calib_width_) / static_cast<double>(image_width_);
    stereo_calib_->getRectificationMaps(map1_left_, map2_left_, map1_right_, map2_right_);
    calibration_loaded_ = true;

    RCLCPP_INFO(this->get_logger(), "Loaded calibration from: %s", stereo_calib_->filePath().string().c_str());
    RCLCPP_INFO(this->get_logger(), "Calibration resolution: %dx%d", calib_width_, calib_height_);
    RCLCPP_INFO(this->get_logger(), "Scale: input %dx%d -> calib %dx%d (scale=%.3f)",
                image_width_, image_height_, calib_width_, calib_height_, depth_scale_);
    RCLCPP_INFO(this->get_logger(), "Stereo parameters: focal=%.2f px, baseline=%.4f m (%.1f mm)",
                focal_length_, baseline_, baseline_ * 1000.0);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(this->get_logger(), "Calibration error: %s", e.what());
    throw;
  }

  // Initialize VPI
  if (!initializeVPI()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to initialize VPI");
    throw std::runtime_error("VPI initialization failed");
  }
  vpi_initialized_ = true;
  RCLCPP_INFO(this->get_logger(), "VPI initialized successfully (SGM CUDA, diagonals=%d)", include_diagonals_);

  // Create publishers (lazy publishing — only publish when subscribed)
  auto sensor_qos = rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort);
  depth_pub_                    = this->create_publisher<sensor_msgs::msg::Image>(depth_topic_, sensor_qos);
  disparity_pub_                = this->create_publisher<stereo_msgs::msg::DisparityImage>(disparity_topic_, sensor_qos);
  disparity_unfiltered_pub_     = this->create_publisher<stereo_msgs::msg::DisparityImage>(disparity_unfiltered_topic_, sensor_qos);
  left_rectified_pub_           = this->create_publisher<sensor_msgs::msg::Image>(left_rectified_topic_, sensor_qos);
  right_rectified_pub_          = this->create_publisher<sensor_msgs::msg::Image>(right_rectified_topic_, sensor_qos);
  left_rectified_color_pub_     = this->create_publisher<sensor_msgs::msg::Image>(left_rectified_color_topic_, sensor_qos);
  left_rectified_compressed_pub_= this->create_publisher<sensor_msgs::msg::CompressedImage>(left_rectified_compressed_topic_, sensor_qos);
  pointcloud_pub_               = this->create_publisher<sensor_msgs::msg::PointCloud2>(pointcloud_topic_, sensor_qos);
  pointcloud_color_pub_         = this->create_publisher<sensor_msgs::msg::PointCloud2>(pointcloud_color_topic_, sensor_qos);

  RCLCPP_INFO(this->get_logger(), "Publishers created (lazy publishing - only publish when subscribed)");
  RCLCPP_INFO(this->get_logger(), "  Point cloud: %s (decimation: %d)", pointcloud_topic_.c_str(), pointcloud_decimation_);
  RCLCPP_INFO(this->get_logger(), "  Point cloud color: %s", pointcloud_color_topic_.c_str());

  logFilterConfig();

  // Synchronized subscriptions for left and right images
  auto qos = rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort);
  left_sub_ = std::make_shared<message_filters::Subscriber<sensor_msgs::msg::Image>>(
      this, left_topic_, qos.get_rmw_qos_profile());
  right_sub_ = std::make_shared<message_filters::Subscriber<sensor_msgs::msg::Image>>(
      this, right_topic_, qos.get_rmw_qos_profile());
  sync_ = std::make_shared<Synchronizer>(SyncPolicy(5), *left_sub_, *right_sub_);
  sync_->registerCallback(std::bind(&StereoDepthEstimator::syncCallback, this,
      std::placeholders::_1, std::placeholders::_2));

  last_stats_time_ = this->now();
  RCLCPP_INFO(this->get_logger(), "Stereo Depth Estimator initialized successfully");
}

// =============================================================================
// Destructor
// =============================================================================
StereoDepthEstimator::~StereoDepthEstimator()
{
  RCLCPP_INFO(this->get_logger(), "Shutting down Stereo Depth Estimator...");
  cleanupVPI();
  RCLCPP_INFO(this->get_logger(), "Stereo Depth Estimator shutdown complete");
}

// =============================================================================
// Synchronized callback — decode ROS images, gate frame rate, call pipeline
// =============================================================================
void StereoDepthEstimator::syncCallback(
    const sensor_msgs::msg::Image::ConstSharedPtr& left_msg,
    const sensor_msgs::msg::Image::ConstSharedPtr& right_msg)
{
  if (!vpi_initialized_ || !calibration_loaded_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "VPI or calibration not ready, skipping frame");
    return;
  }

  // Frame rate control
  input_frame_count_++;
  if ((input_frame_count_ % process_every_n_frames_) != 0) return;

  // Validate dimensions
  if (static_cast<int>(left_msg->width) != image_width_ ||
      static_cast<int>(left_msg->height) != image_height_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
        "Left image size mismatch: got %dx%d, expected %dx%d",
        left_msg->width, left_msg->height, image_width_, image_height_);
    return;
  }
  if (static_cast<int>(right_msg->width) != image_width_ ||
      static_cast<int>(right_msg->height) != image_height_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
        "Right image size mismatch: got %dx%d, expected %dx%d",
        right_msg->width, right_msg->height, image_width_, image_height_);
    return;
  }

  // Decode left image
  cv::Mat left_frame;
  if (left_msg->encoding == "bgr8") {
    left_frame = cv::Mat(left_msg->height, left_msg->width, CV_8UC3,
                         const_cast<uint8_t*>(left_msg->data.data()), left_msg->step).clone();
  } else if (left_msg->encoding == "rgb8") {
    cv::Mat rgb(left_msg->height, left_msg->width, CV_8UC3,
                const_cast<uint8_t*>(left_msg->data.data()), left_msg->step);
    cv::cvtColor(rgb, left_frame, cv::COLOR_RGB2BGR);
  } else if (left_msg->encoding == "mono8") {
    left_frame = cv::Mat(left_msg->height, left_msg->width, CV_8UC1,
                         const_cast<uint8_t*>(left_msg->data.data()), left_msg->step).clone();
  } else {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Unsupported left encoding: %s", left_msg->encoding.c_str());
    return;
  }

  // Decode right image
  cv::Mat right_frame;
  if (right_msg->encoding == "bgr8") {
    right_frame = cv::Mat(right_msg->height, right_msg->width, CV_8UC3,
                          const_cast<uint8_t*>(right_msg->data.data()), right_msg->step).clone();
  } else if (right_msg->encoding == "rgb8") {
    cv::Mat rgb(right_msg->height, right_msg->width, CV_8UC3,
                const_cast<uint8_t*>(right_msg->data.data()), right_msg->step);
    cv::cvtColor(rgb, right_frame, cv::COLOR_RGB2BGR);
  } else if (right_msg->encoding == "mono8") {
    right_frame = cv::Mat(right_msg->height, right_msg->width, CV_8UC1,
                          const_cast<uint8_t*>(right_msg->data.data()), right_msg->step).clone();
  } else {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Unsupported right encoding: %s", right_msg->encoding.c_str());
    return;
  }

  try {
    processFrame(left_frame, right_frame, rclcpp::Time(left_msg->header.stamp));
    frame_count_++;
    if (frame_count_ % 100 == 0) {
      auto now = this->now();
      double elapsed = (now - last_stats_time_).seconds();
      RCLCPP_INFO(this->get_logger(), "Depth estimation: %.1f FPS, %d frames processed",
                  100.0 / elapsed, frame_count_);
      last_stats_time_ = now;
    }
  } catch (const std::exception& e) {
    RCLCPP_ERROR_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                          "Error processing frame: %s", e.what());
  }
}

// =============================================================================
// Pipeline orchestrator — calls helpers, owns timing
// =============================================================================
void StereoDepthEstimator::processFrame(
    const cv::Mat& left_input, const cv::Mat& right_input,
    const rclcpp::Time& timestamp)
{
  using clock = std::chrono::steady_clock;
  const auto t_start = clock::now();

  // ── Subscriber checks ──────────────────────────────────────────────────
  const bool pub_left_rect       = left_rectified_pub_->get_subscription_count() > 0;
  const bool pub_right_rect      = right_rectified_pub_->get_subscription_count() > 0;
  const bool pub_left_color      = left_rectified_color_pub_->get_subscription_count() > 0;
  const bool pub_left_compressed = left_rectified_compressed_pub_->get_subscription_count() > 0;
  const bool pub_pointcloud      = pointcloud_pub_->get_subscription_count() > 0;
  const bool pub_pointcloud_color= pointcloud_color_pub_->get_subscription_count() > 0;
  const bool pub_unfiltered      = disparity_unfiltered_pub_->get_subscription_count() > 0;
  const bool pub_disparity       = disparity_pub_->get_subscription_count() > 0;
  const bool pub_depth           = depth_pub_->get_subscription_count() > 0;
  const auto t_sub = clock::now();

  const bool has_color     = left_input.channels() == 3;
  const bool need_color_rect = has_color &&
      (pub_left_color || pub_left_compressed || pub_pointcloud_color);

  // ── Scale to calibration resolution ────────────────────────────────────
  cv::Mat left_scaled, right_scaled;
  scaleToCalibRes(left_input, right_input, left_scaled, right_scaled);
  const auto t_scale = clock::now();

  // ── Rectify mono (gray + remap) ────────────────────────────────────────
  cv::Mat left_rect, right_rect;
  rectifyMono(left_scaled, right_scaled, left_rect, right_rect);
  const auto t_rect = clock::now();

  // ── Submit SGM to GPU (async) ──────────────────────────────────────────
  if (!submitSGM(left_rect, right_rect)) return;
  const auto t_submit = clock::now();

  // ── CPU work while GPU computes ────────────────────────────────────────
  cv::Mat left_color_rect;
  if (need_color_rect) rectifyColor(left_scaled, left_color_rect);
  const auto t_color_remap = clock::now();

  if (pub_left_rect || pub_right_rect)
    publishMonoRectified(left_rect, right_rect, timestamp, pub_left_rect, pub_right_rect);
  const auto t_mono_pub = clock::now();

  if (pub_left_color || pub_left_compressed)
    publishColorRectified(left_color_rect, left_rect, has_color, timestamp,
                          pub_left_color, pub_left_compressed);
  const auto t_color_pub = clock::now();

  // ── Sync GPU ───────────────────────────────────────────────────────────
  syncSGM();
  const auto t_sgm = clock::now();

  // ── Extract disparity from VPI ─────────────────────────────────────────
  cv::Mat disparity_float = extractDisparity();
  if (disparity_float.empty()) { cleanupSGMWraps(); return; }
  const auto t_extract = clock::now();

  // ── Publish unfiltered disparity ───────────────────────────────────────
  if (pub_unfiltered) publishDisparityMsg(disparity_float, timestamp, disparity_unfiltered_pub_);
  const auto t_unfilt = clock::now();

  // ── Filter chain ───────────────────────────────────────────────────────
  cv::Mat disparity_lowres;
  FilterTimings ft;
  applyFilterChain(disparity_float, disparity_lowres, ft,
                            static_cast<float>(focal_length_), static_cast<float>(baseline_));
  const auto t_filter = clock::now();

  // ── Publish filtered disparity ─────────────────────────────────────────
  if (pub_disparity) publishDisparityMsg(disparity_float, timestamp, disparity_pub_);

  // ── Depth ──────────────────────────────────────────────────────────────
  if (pub_depth) publishDepth(disparity_float, timestamp);
  const auto t_depth = clock::now();

  // ── Point clouds ───────────────────────────────────────────────────────
  if (pub_pointcloud)       publishPointCloudXYZ(disparity_lowres, timestamp);
  if (pub_pointcloud_color) publishPointCloudColor(disparity_lowres, left_color_rect, timestamp);
  const auto t_pc = clock::now();

  // ── Cleanup per-frame VPI wraps ────────────────────────────────────────
  cleanupSGMWraps();
  const auto t_end = clock::now();

  // ── Pipeline timing (1 Hz) ─────────────────────────────────────────────
  auto ms = [](std::chrono::steady_clock::duration d) {
    return std::chrono::duration<double, std::milli>(d).count();
  };

  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "Pipeline %.1fms | sub %.1f | scale %.1f | rectify %.1f | sgm_submit %.1f | "
    "color_remap %.1f | mono_pub %.1f | color_pub %.1f | sgm_sync %.1f | "
    "extract %.1f | unfilt %.1f | filter %.1f | depth %.1f | pc %.1f | "
    "subs[L:%d R:%d C:%d J:%d PC:%d PCC:%d D:%d Di:%d Du:%d col:%d]",
    ms(t_end - t_start),
    ms(t_sub - t_start), ms(t_scale - t_sub), ms(t_rect - t_scale),
    ms(t_submit - t_rect), ms(t_color_remap - t_submit),
    ms(t_mono_pub - t_color_remap), ms(t_color_pub - t_mono_pub),
    ms(t_sgm - t_color_pub), ms(t_extract - t_sgm),
    ms(t_unfilt - t_extract), ms(t_filter - t_unfilt),
    ms(t_depth - t_filter), ms(t_pc - t_depth),
    (int)pub_left_rect, (int)pub_right_rect, (int)pub_left_color,
    (int)pub_left_compressed, (int)pub_pointcloud, (int)pub_pointcloud_color,
    (int)pub_depth, (int)pub_disparity, (int)pub_unfiltered, (int)has_color);

  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "Filter detail %.1fms | down %.1f | clamp %.1f | domain %.1f | speckle %.1f | "
    "edge %.1f | median %.1f | bilateral %.1f | hole %.1f | temporal %.1f | up %.1f",
    ms(t_filter - t_unfilt),
    ft.downsample_ms, ft.depth_clamp_ms, ft.domain_transform_ms,
    ft.speckle_ms, ft.edge_inv_ms, ft.median_ms, ft.bilateral_ms,
    ft.hole_fill_ms, ft.temporal_ms, ft.upsample_ms);
}

} // namespace maurice_cam

// Register the component
RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::StereoDepthEstimator)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  try {
    auto node = std::make_shared<maurice_cam::StereoDepthEstimator>();
    rclcpp::spin(node);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(rclcpp::get_logger("main"), "Exception: %s", e.what());
    return 1;
  }
  rclcpp::shutdown();
  return 0;
}
#endif
