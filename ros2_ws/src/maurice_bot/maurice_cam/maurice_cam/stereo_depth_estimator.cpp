#include "maurice_cam/stereo_depth_estimator.hpp"
#include <turbojpeg.h>

using namespace std::chrono_literals;

// Macro to check VPI status and log errors
#define CHECK_VPI_STATUS(status, msg) \
  if ((status) != VPI_SUCCESS) { \
    char vpi_err[256]; \
    vpiGetLastStatusMessage(vpi_err, sizeof(vpi_err)); \
    RCLCPP_ERROR(this->get_logger(), "%s: %s", msg, vpi_err); \
    return false; \
  }

namespace maurice_cam
{

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
  this->declare_parameter<int>("image_width", 640);   // Single camera image width
  this->declare_parameter<int>("image_height", 480);  // Single camera image height
  this->declare_parameter<int>("process_every_n_frames", 1);  // 1 = process every frame
  this->declare_parameter<std::string>("pointcloud_topic", "/mars/main_camera/points");
  this->declare_parameter<std::string>("pointcloud_color_topic", "/mars/main_camera/points_color");
  this->declare_parameter<int>("pointcloud_decimation", 2);  // 2 = half resolution point cloud
  
  // VPI Stereo Disparity Estimator - Creation parameters (set at payload creation)
  this->declare_parameter<int>("max_disparity", 64);
  this->declare_parameter<int>("include_diagonals", 1);  // 0=H+V only, 1=include diagonals
  
  // VPI Stereo Disparity Estimator - Runtime parameters (CUDA backend)
  this->declare_parameter<int>("confidence_threshold", 32767);
  this->declare_parameter<int>("min_disparity", 0);
  this->declare_parameter<int>("p1", 3);          // Penalty for +/-1 disparity change
  this->declare_parameter<int>("p2", 48);         // Penalty for >1 disparity change (must be < 256)
  this->declare_parameter<double>("uniqueness", 0.15);  // 15% margin (matching stereo_image_proc)
  this->declare_parameter<int>("disparity_border_margin", 10);  // Zero out N pixels from border

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
  
  // VPI creation parameters
  max_disparity_ = this->get_parameter("max_disparity").as_int();
  include_diagonals_ = this->get_parameter("include_diagonals").as_int();
  
  // Clamp max_disparity to CUDA valid range (1-256)
  if (max_disparity_ < 1) max_disparity_ = 1;
  if (max_disparity_ > 256) max_disparity_ = 256;
  
  // VPI runtime parameters (CUDA backend)
  confidence_threshold_ = this->get_parameter("confidence_threshold").as_int();
  min_disparity_ = this->get_parameter("min_disparity").as_int();
  p1_ = this->get_parameter("p1").as_int();
  p2_ = this->get_parameter("p2").as_int();
  uniqueness_ = this->get_parameter("uniqueness").as_double();
  disparity_border_margin_ = this->get_parameter("disparity_border_margin").as_int();
  
  // Validate CUDA constraints: p2 must be < 256
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

  // Find calibration config directory and load calibration
  try {
    stereo_calib_ = StereoCalibration::load(data_directory_);
    calib_width_  = stereo_calib_->calibWidth();
    calib_height_ = stereo_calib_->calibHeight();
    focal_length_ = stereo_calib_->focalLength();
    baseline_     = stereo_calib_->baseline();

    // Calculate depth scale factor (input resolution -> calibration resolution)
    depth_scale_ = static_cast<double>(calib_width_) / static_cast<double>(image_width_);

    // Compute rectification maps at calibration resolution
    stereo_calib_->getRectificationMaps(map1_left_, map2_left_, map1_right_, map2_right_);

    calibration_loaded_ = true;
    RCLCPP_INFO(this->get_logger(), "Loaded calibration from: %s", stereo_calib_->filePath().string().c_str());
    RCLCPP_INFO(this->get_logger(), "Calibration resolution: %dx%d", calib_width_, calib_height_);
    RCLCPP_INFO(this->get_logger(), "Depth scale factor: %.2f (input -> calib)", depth_scale_);
    RCLCPP_INFO(this->get_logger(), "Scale: input %dx%d -> calib %dx%d (scale=%.3f)",
                image_width_, image_height_, calib_width_, calib_height_, depth_scale_);
    RCLCPP_INFO(this->get_logger(), "Stereo parameters: focal=%.2f px, baseline=%.4f m (%.1f mm)",
                focal_length_, baseline_, baseline_ * 1000.0);
    RCLCPP_INFO(this->get_logger(), "Rectification maps computed at %dx%d", calib_width_, calib_height_);
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

  // Create publishers (all topics created, lazy publishing based on subscriber count)
  auto sensor_qos = rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort);
  
  depth_pub_ = this->create_publisher<sensor_msgs::msg::Image>(depth_topic_, sensor_qos);
  disparity_pub_ = this->create_publisher<stereo_msgs::msg::DisparityImage>(disparity_topic_, sensor_qos);
  disparity_unfiltered_pub_ = this->create_publisher<stereo_msgs::msg::DisparityImage>(disparity_unfiltered_topic_, sensor_qos);
  left_rectified_pub_ = this->create_publisher<sensor_msgs::msg::Image>(left_rectified_topic_, sensor_qos);
  right_rectified_pub_ = this->create_publisher<sensor_msgs::msg::Image>(right_rectified_topic_, sensor_qos);
  left_rectified_color_pub_ = this->create_publisher<sensor_msgs::msg::Image>(left_rectified_color_topic_, sensor_qos);
  left_rectified_compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(left_rectified_compressed_topic_, sensor_qos);
  pointcloud_pub_ = this->create_publisher<sensor_msgs::msg::PointCloud2>(pointcloud_topic_, sensor_qos);
  pointcloud_color_pub_ = this->create_publisher<sensor_msgs::msg::PointCloud2>(pointcloud_color_topic_, sensor_qos);
  
  RCLCPP_INFO(this->get_logger(), "Publishers created (lazy publishing - only publish when subscribed)");
  RCLCPP_INFO(this->get_logger(), "  Depth: %s", depth_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Disparity (filtered): %s", disparity_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Disparity (raw): %s", disparity_unfiltered_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Left rectified: %s", left_rectified_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Right rectified: %s", right_rectified_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Left rect color: %s", left_rectified_color_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Left rect compressed: %s", left_rectified_compressed_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Point cloud: %s (decimation: %d)", pointcloud_topic_.c_str(), pointcloud_decimation_);
  RCLCPP_INFO(this->get_logger(), "  Point cloud color: %s", pointcloud_color_topic_.c_str());

  logFilterConfig();

  // Create synchronized subscriptions for left and right images
  auto qos = rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort);
  
  left_sub_ = std::make_shared<message_filters::Subscriber<sensor_msgs::msg::Image>>(
    this, left_topic_, qos.get_rmw_qos_profile());
  right_sub_ = std::make_shared<message_filters::Subscriber<sensor_msgs::msg::Image>>(
    this, right_topic_, qos.get_rmw_qos_profile());
  
  // Use ApproximateTime sync with queue size 5
  sync_ = std::make_shared<Synchronizer>(SyncPolicy(5), *left_sub_, *right_sub_);
  sync_->registerCallback(std::bind(&StereoDepthEstimator::syncCallback, this,
    std::placeholders::_1, std::placeholders::_2));

  last_stats_time_ = this->now();
  RCLCPP_INFO(this->get_logger(), "Stereo Depth Estimator initialized successfully");
}

StereoDepthEstimator::~StereoDepthEstimator()
{
  RCLCPP_INFO(this->get_logger(), "Shutting down Stereo Depth Estimator...");
  cleanupVPI();
  RCLCPP_INFO(this->get_logger(), "Stereo Depth Estimator shutdown complete");
}

bool StereoDepthEstimator::initializeVPI()
{
  VPIStatus status;

  // Create VPI stream with CUDA backend
  status = vpiStreamCreate(VPI_BACKEND_CUDA, &vpi_stream_);
  CHECK_VPI_STATUS(status, "Failed to create VPI stream");

  // Create disparity output image at calibration resolution
  // S16 format, Q10.5 fixed point (divide by 32 to get pixels)
  // Include CPU backend for reading results back
  status = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_S16,
                          VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_disparity_);
  CHECK_VPI_STATUS(status, "Failed to create disparity image");

  // Create confidence map
  status = vpiImageCreate(calib_width_, calib_height_, VPI_IMAGE_FORMAT_U16,
                          VPI_BACKEND_CUDA | VPI_BACKEND_CPU, &vpi_confidence_);
  CHECK_VPI_STATUS(status, "Failed to create confidence image");

  // Create stereo disparity estimator payload for SGM (CUDA backend)
  VPIStereoDisparityEstimatorCreationParams stereo_params;
  vpiInitStereoDisparityEstimatorCreationParams(&stereo_params);
  stereo_params.maxDisparity = max_disparity_;
  // Include diagonal paths for higher quality SGM (slower, more memory)
  stereo_params.includeDiagonals = include_diagonals_;

  status = vpiCreateStereoDisparityEstimator(VPI_BACKEND_CUDA, calib_width_, calib_height_,
                                              VPI_IMAGE_FORMAT_U8, &stereo_params, &stereo_payload_);
  CHECK_VPI_STATUS(status, "Failed to create stereo disparity estimator");

  RCLCPP_INFO(this->get_logger(), "VPI SGM CUDA created: %dx%d, maxDisp=%d, diagonals=%d",
              calib_width_, calib_height_, max_disparity_, include_diagonals_);
  return true;
}

void StereoDepthEstimator::cleanupVPI()
{
  // Wait for pending operations
  if (vpi_stream_) {
    vpiStreamSync(vpi_stream_);
  }

  // Destroy payload
  if (stereo_payload_) vpiPayloadDestroy(stereo_payload_);

  // Destroy images
  if (vpi_disparity_) vpiImageDestroy(vpi_disparity_);
  if (vpi_confidence_) vpiImageDestroy(vpi_confidence_);

  // Destroy stream
  if (vpi_stream_) vpiStreamDestroy(vpi_stream_);

  vpi_stream_ = nullptr;
  stereo_payload_ = nullptr;
  vpi_disparity_ = nullptr;
  vpi_confidence_ = nullptr;
}

void StereoDepthEstimator::syncCallback(
    const sensor_msgs::msg::Image::ConstSharedPtr& left_msg,
    const sensor_msgs::msg::Image::ConstSharedPtr& right_msg)
{
  if (!vpi_initialized_ || !calibration_loaded_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "VPI or calibration not ready, skipping frame");
    return;
  }

  // Frame rate control - only process 1 out of every N frames
  input_frame_count_++;
  if ((input_frame_count_ % process_every_n_frames_) != 0) {
    return;  // Skip this frame
  }

  // Validate image dimensions
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

  // Wrap ROS message data as cv::Mat
  cv::Mat left_frame, right_frame;
  
  // Process left image
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

  // Process right image
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

  // Process the frame
  try {
    processFrame(left_frame, right_frame, rclcpp::Time(left_msg->header.stamp));
    frame_count_++;

    // Print stats every 100 frames
    if (frame_count_ % 100 == 0) {
      auto now = this->now();
      double elapsed = (now - last_stats_time_).seconds();
      double fps = 100.0 / elapsed;
      RCLCPP_INFO(this->get_logger(), "Depth estimation: %.1f FPS, %d frames processed",
                  fps, frame_count_);
      last_stats_time_ = now;
    }
  } catch (const std::exception& e) {
    RCLCPP_ERROR_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                          "Error processing frame: %s", e.what());
  }
}

void StereoDepthEstimator::processFrame(const cv::Mat& left_input, const cv::Mat& right_input, const rclcpp::Time& timestamp)
{
  using clock = std::chrono::steady_clock;
  const auto t_frame_start = clock::now();

  // ===== Subscription checks (all in one place) =====
  const bool pub_left_rect = left_rectified_pub_->get_subscription_count() > 0;
  const bool pub_right_rect = right_rectified_pub_->get_subscription_count() > 0;
  const bool pub_left_rect_color = left_rectified_color_pub_->get_subscription_count() > 0;
  const bool pub_left_rect_compressed = left_rectified_compressed_pub_->get_subscription_count() > 0;
  const bool pub_pointcloud = pointcloud_pub_->get_subscription_count() > 0;
  const bool pub_pointcloud_color = pointcloud_color_pub_->get_subscription_count() > 0;
  const bool pub_unfiltered = disparity_unfiltered_pub_->get_subscription_count() > 0;
  const bool pub_disparity = disparity_pub_->get_subscription_count() > 0;
  const bool pub_depth = depth_pub_->get_subscription_count() > 0;
  const auto t_sub_check = clock::now();

  // ===== Derived action flags =====
  const bool has_color_input = left_input.channels() == 3;
  const bool need_mono_rect_pub = pub_left_rect || pub_right_rect;
  const bool need_color_rect = has_color_input &&
      (pub_left_rect_color || pub_left_rect_compressed || pub_pointcloud_color);
  const bool need_any_pointcloud = pub_pointcloud || pub_pointcloud_color;

  // ===== STEP 1: Scale to calibration resolution =====
  // Input images are already in the correct orientation (matching calibration)
  cv::Mat left_scaled, right_scaled;
  cv::resize(left_input, left_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_input, right_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  const auto t_scale = clock::now();

  // ===== STEP 2: Convert to grayscale =====
  cv::Mat left_gray, right_gray;
  if (left_input.channels() == 3) {
    cv::cvtColor(left_scaled, left_gray, cv::COLOR_BGR2GRAY);
    cv::cvtColor(right_scaled, right_gray, cv::COLOR_BGR2GRAY);
  } else {
    left_gray = left_scaled;
    right_gray = right_scaled;
  }
  const auto t_gray = clock::now();

  // ===== STEP 3: Rectify using OpenCV =====
  cv::Mat left_rect, right_rect;
  cv::remap(left_gray, left_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
  cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);
  const auto t_rectify = clock::now();

  // ===== STEP 4: Wrap rectified images for VPI and submit SGM (CUDA) =====
  // Submit GPU work FIRST, then do CPU work (color remap, publishing) while GPU runs.
  VPIImage vpi_left_wrap = nullptr;
  VPIImage vpi_right_wrap = nullptr;
  VPIStatus status;

  status = vpiImageCreateWrapperOpenCVMat(left_rect, VPI_IMAGE_FORMAT_U8,
                                           VPI_BACKEND_CUDA, &vpi_left_wrap);
  if (status != VPI_SUCCESS) {
    RCLCPP_ERROR(this->get_logger(), "Failed to wrap left rectified image");
    return;
  }

  status = vpiImageCreateWrapperOpenCVMat(right_rect, VPI_IMAGE_FORMAT_U8,
                                           VPI_BACKEND_CUDA, &vpi_right_wrap);
  if (status != VPI_SUCCESS) {
    vpiImageDestroy(vpi_left_wrap);
    RCLCPP_ERROR(this->get_logger(), "Failed to wrap right rectified image");
    return;
  }

  const auto t_vpi_wrap = clock::now();

  VPIStereoDisparityEstimatorParams stereo_params;
  vpiInitStereoDisparityEstimatorParams(&stereo_params);
  stereo_params.maxDisparity = max_disparity_;
  stereo_params.confidenceThreshold = confidence_threshold_;
  stereo_params.minDisparity = min_disparity_;
  stereo_params.p1 = p1_;
  stereo_params.p2 = p2_;
  stereo_params.uniqueness = static_cast<float>(uniqueness_);

  vpiStreamSync(vpi_stream_);
  status = vpiSubmitStereoDisparityEstimator(vpi_stream_, VPI_BACKEND_CUDA, stereo_payload_,
                                              vpi_left_wrap, vpi_right_wrap,
                                              vpi_disparity_, vpi_confidence_, &stereo_params);
  if (status != VPI_SUCCESS) {
    vpiImageDestroy(vpi_left_wrap);
    vpiImageDestroy(vpi_right_wrap);
    RCLCPP_ERROR(this->get_logger(), "Failed to compute stereo disparity");
    return;
  }
  const auto t_sgm_submit = clock::now();

  // ===== STEP 5: CPU work while GPU computes disparity =====
  // Color remap + rectified image publishing overlaps with SGM on the GPU.

  // Rectify the color image at calibration resolution if anyone needs it
  // (color rect topic, compressed rect topic, or color point cloud).
  // The default pointcloud (xyz-only) does NOT need color rectification.
  cv::Mat left_color_rect;  // calib-resolution, BGR, rectified
  if (need_color_rect) {
    cv::remap(left_scaled, left_color_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
  }
  const auto t_color_remap = clock::now();

  if (need_mono_rect_pub) {
    // Upscale rectified images to input resolution
    cv::Mat left_rect_full, right_rect_full;
    cv::resize(left_rect, left_rect_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
    cv::resize(right_rect, right_rect_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
    
    if (pub_left_rect) {
      auto left_rect_msg = std::make_unique<sensor_msgs::msg::Image>();
      left_rect_msg->header.stamp = timestamp;
      left_rect_msg->header.frame_id = frame_id_;
      left_rect_msg->height = image_height_;
      left_rect_msg->width = image_width_;
      left_rect_msg->encoding = "mono8";
      left_rect_msg->is_bigendian = false;
      left_rect_msg->step = image_width_;
      left_rect_msg->data.resize(left_rect_msg->height * left_rect_msg->step);
      memcpy(left_rect_msg->data.data(), left_rect_full.data, left_rect_msg->data.size());
      left_rectified_pub_->publish(std::move(left_rect_msg));
    }
    
    if (pub_right_rect) {
      auto right_rect_msg = std::make_unique<sensor_msgs::msg::Image>();
      right_rect_msg->header.stamp = timestamp;
      right_rect_msg->header.frame_id = frame_id_;
      right_rect_msg->height = image_height_;
      right_rect_msg->width = image_width_;
      right_rect_msg->encoding = "mono8";
      right_rect_msg->is_bigendian = false;
      right_rect_msg->step = image_width_;
      right_rect_msg->data.resize(right_rect_msg->height * right_rect_msg->step);
      memcpy(right_rect_msg->data.data(), right_rect_full.data, right_rect_msg->data.size());
      right_rectified_pub_->publish(std::move(right_rect_msg));
    }
  }
  const auto t_mono_pub = clock::now();

  // Publish color rectified + compressed if subscribed
  auto t_color_pub = clock::now();
  if (pub_left_rect_color || pub_left_rect_compressed) {
    cv::Mat left_color_rect_src;
    if (has_color_input) {
      left_color_rect_src = left_color_rect;  // already rectified above
    } else {
      // If input was mono, convert rectified mono to BGR
      cv::cvtColor(left_rect, left_color_rect_src, cv::COLOR_GRAY2BGR);
    }
    cv::Mat left_color_rect_full;
    cv::resize(left_color_rect_src, left_color_rect_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);

    if (pub_left_rect_color) {
      auto color_msg = std::make_unique<sensor_msgs::msg::Image>();
      color_msg->header.stamp = timestamp;
      color_msg->header.frame_id = frame_id_;
      color_msg->height = image_height_;
      color_msg->width = image_width_;
      color_msg->encoding = "bgr8";
      color_msg->is_bigendian = false;
      color_msg->step = image_width_ * 3;
      color_msg->data.resize(color_msg->height * color_msg->step);
      memcpy(color_msg->data.data(), left_color_rect_full.data, color_msg->data.size());
      left_rectified_color_pub_->publish(std::move(color_msg));
    }
    t_color_pub = clock::now();

    if (pub_left_rect_compressed) {
      // Encode to JPEG using TurboJPEG
      auto compressed_msg = std::make_unique<sensor_msgs::msg::CompressedImage>();
      compressed_msg->header.stamp = timestamp;
      compressed_msg->header.frame_id = frame_id_;
      compressed_msg->format = "jpeg";

      tjhandle tj = tjInitCompress();
      if (tj) {
        unsigned char* jpeg_buf = nullptr;
        unsigned long jpeg_size = 0;
        int rc = tjCompress2(tj, left_color_rect_full.data,
                             left_color_rect_full.cols, 0, left_color_rect_full.rows,
                             TJPF_BGR, &jpeg_buf, &jpeg_size,
                             TJSAMP_420, jpeg_quality_, TJFLAG_FASTDCT);
        if (rc == 0 && jpeg_buf) {
          compressed_msg->data.assign(jpeg_buf, jpeg_buf + jpeg_size);
          left_rectified_compressed_pub_->publish(std::move(compressed_msg));
        }
        if (jpeg_buf) tjFree(jpeg_buf);
        tjDestroy(tj);
      }
    }
  }
  const auto t_pub_rect = clock::now();

  // ===== STEP 6: Synchronize GPU (SGM should be done or nearly done) =====
  vpiStreamSync(vpi_stream_);
  const auto t_sgm = clock::now();

  // ===== STEP 7: Lock disparity and convert to depth using reprojectImageTo3D =====
  VPIImageData disparity_data;
  status = vpiImageLockData(vpi_disparity_, VPI_LOCK_READ, VPI_IMAGE_BUFFER_HOST_PITCH_LINEAR, &disparity_data);
  if (status != VPI_SUCCESS) {
    vpiImageDestroy(vpi_left_wrap);
    vpiImageDestroy(vpi_right_wrap);
    RCLCPP_ERROR(this->get_logger(), "Failed to lock disparity image");
    return;
  }

  // VPI disparity is in Q10.5 format (divide by 32 to get pixels)
  const float DISPARITY_SCALE = 32.0f;

  // Copy VPI disparity to OpenCV Mat and convert to float pixels
  const int16_t* disp_ptr = reinterpret_cast<const int16_t*>(disparity_data.buffer.pitch.planes[0].data);
  const int disp_pitch = disparity_data.buffer.pitch.planes[0].pitchBytes / sizeof(int16_t);
  
  // VPI CUDA writes INT16_MAX (32767) for invalid / low-confidence pixels.
  // After /32 that becomes ~1024, far above any real disparity.
  // Clamp those to 0 (invalid) so downstream filters and depth math are clean.
  const float max_valid_disparity = static_cast<float>(max_disparity_);

  cv::Mat disparity_float(calib_height_, calib_width_, CV_32FC1);
  for (int y = 0; y < calib_height_; y++) {
    const int16_t* disp_row = disp_ptr + y * disp_pitch;
    float* float_row = disparity_float.ptr<float>(y);
    for (int x = 0; x < calib_width_; x++) {
      float d = static_cast<float>(disp_row[x]) / DISPARITY_SCALE;
      float_row[x] = (d >= max_valid_disparity || d <= 0.0f) ? 0.0f : d;
    }
  }

  vpiImageUnlock(vpi_disparity_);
  const auto t_lock_copy = clock::now();

  // Zero out border pixels to eliminate edge artifacts from stereo matching
  if (disparity_border_margin_ > 0) {
    const int margin = disparity_border_margin_;
    for (int y = 0; y < margin && y < calib_height_; y++) {
      float* top_row = disparity_float.ptr<float>(y);
      float* bot_row = disparity_float.ptr<float>(calib_height_ - 1 - y);
      for (int x = 0; x < calib_width_; x++) {
        top_row[x] = 0.0f;
        bot_row[x] = 0.0f;
      }
    }
    for (int y = margin; y < calib_height_ - margin; y++) {
      float* row = disparity_float.ptr<float>(y);
      for (int x = 0; x < margin && x < calib_width_; x++) {
        row[x] = 0.0f;
        row[calib_width_ - 1 - x] = 0.0f;
      }
    }
  }
  const auto t_border = clock::now();

  // ===== STEP 7a: Publish unfiltered disparity if subscribed =====
  if (pub_unfiltered) {
    publishDisparityMsg(disparity_float, timestamp, disparity_unfiltered_pub_);
  }
  const auto t_convert = clock::now();

  // ===== STEP 7b: Apply disparity filter chain (in-place) =====
  cv::Mat disparity_lowres;  // quarter-res filtered disparity for point cloud
  FilterTimings filter_timings;
  applyFilterChain(disparity_float, disparity_lowres, filter_timings);
  const auto t_filter = clock::now();

  // ===== STEP 7c: Publish filtered disparity if subscribed =====
  if (pub_disparity) {
    publishDisparityMsg(disparity_float, timestamp, disparity_pub_);
  }

  // ===== STEP 8: Generate and publish depth (only if subscribed) =====
  const float MAX_DEPTH_M = 10.0f;
  if (pub_depth) {
    const float f_calib = static_cast<float>(focal_length_);
    const float abs_baseline = std::abs(static_cast<float>(baseline_));
    const float fb = f_calib * abs_baseline;  // focal_length * baseline in meters

    cv::Mat depth_calib(calib_height_, calib_width_, CV_16SC1);
    for (int y = 0; y < calib_height_; y++) {
      const float* disp_row = disparity_float.ptr<float>(y);
      int16_t* depth_row = depth_calib.ptr<int16_t>(y);
      for (int x = 0; x < calib_width_; x++) {
        const float d = disp_row[x];
        if (d > 0.0f) {
          float z = fb / d;  // depth in meters
          if (z > 0.0f && z <= MAX_DEPTH_M) {
            depth_row[x] = static_cast<int16_t>(std::clamp(z * 1000.0f, -32768.0f, 32767.0f));
          } else {
            depth_row[x] = 0;
          }
        } else {
          depth_row[x] = 0;
        }
      }
    }

    cv::Mat depth_full;
    cv::resize(depth_calib, depth_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);

    auto depth_msg = std::make_unique<sensor_msgs::msg::Image>();
    depth_msg->header.stamp = timestamp;
    depth_msg->header.frame_id = frame_id_;
    depth_msg->height = image_height_;
    depth_msg->width = image_width_;
    depth_msg->encoding = "16SC1";
    depth_msg->is_bigendian = false;
    depth_msg->step = image_width_ * sizeof(int16_t);
    depth_msg->data.resize(depth_msg->height * depth_msg->step);
    memcpy(depth_msg->data.data(), depth_full.data, depth_msg->data.size());
    depth_pub_->publish(std::move(depth_msg));
  }
  const auto t_depth = clock::now();

  // ===== STEP 9: Generate point cloud directly from filtered disparity =====
  // Use the disparity at its current resolution (quarter after filter downsample).
  // This keeps the point count low and avoids an expensive upscale+decimate round-trip.
  auto t_pc_resize = clock::now();
  auto t_pc_alloc = t_pc_resize;
  auto t_pc_fill = t_pc_resize;
  auto t_pc_pub = t_pc_resize;
  if (need_any_pointcloud) {
    const int disp_w = disparity_lowres.cols;
    const int disp_h = disparity_lowres.rows;

    // Scale pixel-coordinate intrinsics (fx, fy, cx, cy) to the downsampled grid.
    // But NOT the focal length used for depth: disparity values are still in
    // calibration-resolution pixels (resize averages values, doesn't rescale them),
    // so depth = f_calib * baseline / d must use the original focal length.
    const float scale_to_disp = static_cast<float>(disp_w) / static_cast<float>(calib_width_);
    const float fx = static_cast<float>(stereo_calib_->P1().at<double>(0, 0)) * scale_to_disp;
    const float fy = static_cast<float>(stereo_calib_->P1().at<double>(1, 1)) * scale_to_disp;
    const float cx = static_cast<float>(stereo_calib_->P1().at<double>(0, 2)) * scale_to_disp;
    const float cy = static_cast<float>(stereo_calib_->P1().at<double>(1, 2)) * scale_to_disp;
    const float f_depth = static_cast<float>(focal_length_);  // calib-res, matches disparity units
    const float baseline = static_cast<float>(baseline_);

    // Downsample rectified colour image to match disparity resolution (only for color PC).
    // Must use the rectified image (not raw input) so colours align with disparity.
    cv::Mat color_for_pc;
    const bool has_color = pub_pointcloud_color && need_color_rect && !left_color_rect.empty();
    if (has_color) {
      cv::resize(left_color_rect, color_for_pc, cv::Size(disp_w, disp_h), 0, 0, cv::INTER_AREA);
    }
    t_pc_resize = clock::now();

    // Apply decimation on top of the (already small) disparity
    const int pc_width  = disp_w / pointcloud_decimation_;
    const int pc_height = disp_h / pointcloud_decimation_;
    const int step = pointcloud_decimation_;
    t_pc_alloc = clock::now();

    // --- Default pointcloud: xyz only (no rgb overhead) ---
    if (pub_pointcloud) {
      auto cloud_msg = std::make_unique<sensor_msgs::msg::PointCloud2>();
      cloud_msg->header.stamp = timestamp;
      cloud_msg->header.frame_id = frame_id_;
      cloud_msg->height = pc_height;
      cloud_msg->width = pc_width;
      cloud_msg->is_dense = false;
      cloud_msg->is_bigendian = false;

      sensor_msgs::PointCloud2Modifier modifier(*cloud_msg);
      modifier.setPointCloud2FieldsByString(1, "xyz");
      modifier.resize(pc_width * pc_height);

      sensor_msgs::PointCloud2Iterator<float> iter_x(*cloud_msg, "x");
      sensor_msgs::PointCloud2Iterator<float> iter_y(*cloud_msg, "y");
      sensor_msgs::PointCloud2Iterator<float> iter_z(*cloud_msg, "z");

      for (int v = 0; v < pc_height; ++v) {
        for (int u = 0; u < pc_width; ++u, ++iter_x, ++iter_y, ++iter_z) {
          const int px = u * step;
          const int py = v * step;
          const float d = disparity_lowres.at<float>(py, px);

          if (d > 0.0f && std::isfinite(d)) {
            float z = f_depth * baseline / d;
            if (z > 0.0f && z <= MAX_DEPTH_M) {
              *iter_x = (static_cast<float>(px) - cx) * z / fx;
              *iter_y = (static_cast<float>(py) - cy) * z / fy;
              *iter_z = z;
              continue;
            }
          }
          *iter_x = std::numeric_limits<float>::quiet_NaN();
          *iter_y = std::numeric_limits<float>::quiet_NaN();
          *iter_z = std::numeric_limits<float>::quiet_NaN();
        }
      }
      pointcloud_pub_->publish(std::move(cloud_msg));
    }

    // --- Color pointcloud: xyz + rgb ---
    if (pub_pointcloud_color) {
      auto cloud_msg = std::make_unique<sensor_msgs::msg::PointCloud2>();
      cloud_msg->header.stamp = timestamp;
      cloud_msg->header.frame_id = frame_id_;
      cloud_msg->height = pc_height;
      cloud_msg->width = pc_width;
      cloud_msg->is_dense = false;
      cloud_msg->is_bigendian = false;

      sensor_msgs::PointCloud2Modifier modifier(*cloud_msg);
      modifier.setPointCloud2FieldsByString(2, "xyz", "rgb");
      modifier.resize(pc_width * pc_height);

      sensor_msgs::PointCloud2Iterator<float> iter_x(*cloud_msg, "x");
      sensor_msgs::PointCloud2Iterator<float> iter_y(*cloud_msg, "y");
      sensor_msgs::PointCloud2Iterator<float> iter_z(*cloud_msg, "z");
      sensor_msgs::PointCloud2Iterator<float> iter_rgb(*cloud_msg, "rgb");

      for (int v = 0; v < pc_height; ++v) {
        for (int u = 0; u < pc_width; ++u, ++iter_x, ++iter_y, ++iter_z, ++iter_rgb) {
          const int px = u * step;
          const int py = v * step;
          const float d = disparity_lowres.at<float>(py, px);

          if (d > 0.0f && std::isfinite(d)) {
            float z = f_depth * baseline / d;
            if (z > 0.0f && z <= MAX_DEPTH_M) {
              *iter_x = (static_cast<float>(px) - cx) * z / fx;
              *iter_y = (static_cast<float>(py) - cy) * z / fy;
              *iter_z = z;

              if (has_color) {
                const cv::Vec3b& bgr = color_for_pc.at<cv::Vec3b>(py, px);
                uint32_t rgb_packed = (static_cast<uint32_t>(bgr[2]) << 16) |
                                      (static_cast<uint32_t>(bgr[1]) << 8) |
                                      (static_cast<uint32_t>(bgr[0]));
                float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
                *iter_rgb = rgb_float;
              } else {
                uint32_t rgb_packed = 0x00808080;
                float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
                *iter_rgb = rgb_float;
              }
              continue;
            }
          }
          *iter_x = std::numeric_limits<float>::quiet_NaN();
          *iter_y = std::numeric_limits<float>::quiet_NaN();
          *iter_z = std::numeric_limits<float>::quiet_NaN();
          uint32_t rgb_packed = 0x00000000;
          float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
          *iter_rgb = rgb_float;
        }
      }
      pointcloud_color_pub_->publish(std::move(cloud_msg));
    }
    t_pc_fill = clock::now();

    t_pc_pub = clock::now();
  }
  const auto t_frame_end = clock::now();

  // ===== Pipeline timing (throttled 1 Hz) =====
  auto ms = [](std::chrono::steady_clock::duration d) {
    return std::chrono::duration<double, std::milli>(d).count();
  };
  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "Pipeline %.1fms | sub_check %.1f | scale %.1f | gray %.1f | rectify %.1f | "
    "vpi_wrap %.1f | sgm_submit %.1f | "
    "color_remap %.1f | mono_pub %.1f | color_pub %.1f | jpeg_pub %.1f | "
    "sgm_sync %.1f | "
    "lock+copy %.1f | border %.1f | unfilt_pub %.1f | filter %.1f | depth %.1f | "
    "pc_resize %.1f | pc_alloc %.1f | pc_fill %.1f | pc_pub %.1f | depth_pub %.1f | "
    "subs[L:%d R:%d C:%d J:%d PC:%d PCC:%d D:%d Di:%d Du:%d col:%d]",
    ms(t_frame_end - t_frame_start),
    ms(t_sub_check - t_frame_start),
    ms(t_scale - t_sub_check),
    ms(t_gray - t_scale),
    ms(t_rectify - t_gray),
    ms(t_vpi_wrap - t_rectify),
    ms(t_sgm_submit - t_vpi_wrap),
    ms(t_color_remap - t_sgm_submit),
    ms(t_mono_pub - t_color_remap),
    ms(t_color_pub - t_mono_pub),
    ms(t_pub_rect - t_color_pub),
    ms(t_sgm - t_pub_rect),
    ms(t_lock_copy - t_sgm),
    ms(t_border - t_lock_copy),
    ms(t_convert - t_border),
    ms(t_filter - t_convert),
    ms(t_depth - t_filter),
    ms(t_pc_resize - t_depth),
    ms(t_pc_alloc - t_pc_resize),
    ms(t_pc_fill - t_pc_alloc),
    ms(t_pc_pub - t_pc_fill),
    ms(t_frame_end - t_pc_pub),
    (int)pub_left_rect, (int)pub_right_rect, (int)pub_left_rect_color,
    (int)pub_left_rect_compressed, (int)pub_pointcloud, (int)pub_pointcloud_color,
    (int)pub_depth, (int)pub_disparity, (int)pub_unfiltered, (int)has_color_input);

  // Detailed filter breakdown (throttled separately)
  const auto& ft = filter_timings;
  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "Filter detail %.1fms | down %.1f | clamp %.1f | domain %.1f | speckle %.1f | "
    "edge %.1f | median %.1f | bilateral %.1f | hole %.1f | temporal %.1f | up %.1f",
    ms(t_filter - t_convert),
    ft.downsample_ms, ft.depth_clamp_ms, ft.domain_transform_ms,
    ft.speckle_ms, ft.edge_inv_ms, ft.median_ms, ft.bilateral_ms,
    ft.hole_fill_ms, ft.temporal_ms, ft.upsample_ms);

  // Cleanup temporary wrapped images
  vpiImageDestroy(vpi_left_wrap);
  vpiImageDestroy(vpi_right_wrap);
}

// =============================================================================
// Disparity publishing helper
// =============================================================================
void StereoDepthEstimator::publishDisparityMsg(
    const cv::Mat& disparity_float,
    const rclcpp::Time& timestamp,
    rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr& pub)
{
  cv::Mat disp_full;
  cv::resize(disparity_float, disp_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);

  auto msg = std::make_unique<stereo_msgs::msg::DisparityImage>();
  msg->header.stamp = timestamp;
  msg->header.frame_id = frame_id_;

  msg->image.header = msg->header;
  msg->image.height = image_height_;
  msg->image.width = image_width_;
  msg->image.encoding = "32FC1";
  msg->image.is_bigendian = false;
  msg->image.step = image_width_ * sizeof(float);
  msg->image.data.resize(msg->image.height * msg->image.step);
  memcpy(msg->image.data.data(), disp_full.data, msg->image.data.size());

  const float scale_up = 1.0f / static_cast<float>(depth_scale_);
  msg->f = static_cast<float>(focal_length_) * scale_up;
  msg->t = static_cast<float>(baseline_);
  msg->min_disparity = static_cast<float>(min_disparity_);
  msg->max_disparity = static_cast<float>(max_disparity_);
  msg->delta_d = 1.0f / 32.0f;  // VPI Q10.5

  pub->publish(std::move(msg));
}

// =============================================================================
// Disparity Filter Chain — integrated into depth estimator
// =============================================================================
void StereoDepthEstimator::initFilterParams()
{
  // 0. Downsample — run all filters at reduced resolution
  declare_parameter<bool>("filter_downsample.enabled", true);
  declare_parameter<int>("filter_downsample.factor", 4);
  filter_downsample_enabled_ = get_parameter("filter_downsample.enabled").as_bool();
  filter_downsample_factor_ = std::clamp(
      static_cast<int>(get_parameter("filter_downsample.factor").as_int()), 1, 8);

  // 1. Median
  declare_parameter<bool>("median.enabled", false);
  declare_parameter<int>("median.kernel_size", 5);
  median_enabled_ = get_parameter("median.enabled").as_bool();
  median_kernel_size_ = static_cast<int>(get_parameter("median.kernel_size").as_int());
  if (median_kernel_size_ % 2 == 0) median_kernel_size_ += 1;
  median_kernel_size_ = std::max(3, median_kernel_size_);

  // 2. Bilateral
  declare_parameter<bool>("bilateral.enabled", false);
  declare_parameter<int>("bilateral.diameter", 5);
  declare_parameter<double>("bilateral.sigma_color", 10.0);
  declare_parameter<double>("bilateral.sigma_space", 10.0);
  bilateral_enabled_ = get_parameter("bilateral.enabled").as_bool();
  bilateral_diameter_ = static_cast<int>(get_parameter("bilateral.diameter").as_int());
  bilateral_sigma_color_ = get_parameter("bilateral.sigma_color").as_double();
  bilateral_sigma_space_ = get_parameter("bilateral.sigma_space").as_double();

  // 3. Hole Filling
  declare_parameter<bool>("hole_filling.enabled", false);
  declare_parameter<int>("hole_filling.mode", 1);
  declare_parameter<int>("hole_filling.strategy", 1);
  hole_filling_enabled_ = get_parameter("hole_filling.enabled").as_bool();
  int hf_mode = std::clamp(static_cast<int>(get_parameter("hole_filling.mode").as_int()), 0, 5);
  const int hf_map[] = {0, 2, 4, 8, 16, 9999};
  hole_fill_radius_ = hf_map[hf_mode];
  hole_fill_strategy_ = std::clamp(static_cast<int>(get_parameter("hole_filling.strategy").as_int()), 0, 2);

  // 4. Depth Clamping
  declare_parameter<bool>("depth_clamp.enabled", false);
  declare_parameter<double>("depth_clamp.min_depth_meters", 0.25);
  declare_parameter<double>("depth_clamp.max_depth_meters", 5.0);
  depth_clamp_enabled_ = get_parameter("depth_clamp.enabled").as_bool();
  min_depth_meters_ = get_parameter("depth_clamp.min_depth_meters").as_double();
  max_depth_meters_ = get_parameter("depth_clamp.max_depth_meters").as_double();

  // 5. Edge Invalidation
  declare_parameter<bool>("edge_invalidation.enabled", false);
  declare_parameter<int>("edge_invalidation.width", 3);
  declare_parameter<double>("edge_invalidation.canny_low", 10.0);
  declare_parameter<double>("edge_invalidation.canny_high", 30.0);
  edge_inv_enabled_ = get_parameter("edge_invalidation.enabled").as_bool();
  edge_inv_width_ = std::max(1, static_cast<int>(get_parameter("edge_invalidation.width").as_int()));
  edge_canny_low_ = get_parameter("edge_invalidation.canny_low").as_double();
  edge_canny_high_ = get_parameter("edge_invalidation.canny_high").as_double();

  // 6. Speckle
  declare_parameter<bool>("speckle.enabled", false);
  declare_parameter<int>("speckle.max_size", 200);
  declare_parameter<double>("speckle.max_diff", 1.0);
  speckle_enabled_ = get_parameter("speckle.enabled").as_bool();
  speckle_max_size_ = std::max(1, static_cast<int>(get_parameter("speckle.max_size").as_int()));
  speckle_max_diff_ = std::max(0.0, get_parameter("speckle.max_diff").as_double());

  // 7. Domain Transform
  declare_parameter<bool>("domain_transform.enabled", false);
  declare_parameter<int>("domain_transform.iterations", 2);
  declare_parameter<double>("domain_transform.alpha", 0.5);
  declare_parameter<int>("domain_transform.delta", 20);
  dt_enabled_ = get_parameter("domain_transform.enabled").as_bool();
  dt_iterations_ = std::clamp(static_cast<int>(get_parameter("domain_transform.iterations").as_int()), 1, 5);
  dt_alpha_ = std::clamp(get_parameter("domain_transform.alpha").as_double(), 0.25, 1.0);
  dt_delta_ = std::clamp(static_cast<int>(get_parameter("domain_transform.delta").as_int()), 1, 50);

  // 8. Temporal
  declare_parameter<bool>("temporal.enabled", false);
  declare_parameter<double>("temporal.alpha", 0.4);
  declare_parameter<int>("temporal.delta", 20);
  declare_parameter<int>("temporal.persistence", 3);
  temporal_enabled_ = get_parameter("temporal.enabled").as_bool();
  temporal_alpha_ = std::clamp(get_parameter("temporal.alpha").as_double(), 0.0, 1.0);
  temporal_delta_ = std::clamp(static_cast<int>(get_parameter("temporal.delta").as_int()), 1, 100);
  temporal_persistence_ = std::clamp(static_cast<int>(get_parameter("temporal.persistence").as_int()), 0, 8);

  // Filter execution order
  declare_parameter<std::vector<std::string>>("filter_order", {
    "median", "bilateral", "hole_filling", "depth_clamp",
    "edge_invalidation", "speckle", "domain_transform", "temporal"});
  filter_order_ = get_parameter("filter_order").as_string_array();
}

void StereoDepthEstimator::logFilterConfig()
{
  auto log = [this](const char* name, bool on, const std::string & detail = "") {
    RCLCPP_INFO(get_logger(), "  %-22s %s%s", name, on ? "ON" : "OFF", detail.c_str());
  };

  RCLCPP_INFO(get_logger(), "=== Disparity Filter Chain ===");
  log("Downsample", filter_downsample_enabled_,
      filter_downsample_enabled_ ? "  factor=" + std::to_string(filter_downsample_factor_) : "");

  // Print execution order
  std::string order_str;
  for (const auto& name : filter_order_) {
    if (!order_str.empty()) order_str += " → ";
    order_str += name;
  }
  RCLCPP_INFO(get_logger(), "  Order: %s", order_str.c_str());

  log("Median", median_enabled_,
      median_enabled_ ? "  kernel=" + std::to_string(median_kernel_size_) : "");
  log("Bilateral", bilateral_enabled_,
      bilateral_enabled_ ? "  d=" + std::to_string(bilateral_diameter_) : "");
  log("Hole Filling", hole_filling_enabled_,
      hole_filling_enabled_ ? "  radius=" + std::to_string(hole_fill_radius_)
        + " strategy=" + std::to_string(hole_fill_strategy_) : "");
  log("Depth Clamping", depth_clamp_enabled_,
      depth_clamp_enabled_ ? "  min=" + std::to_string(min_depth_meters_)
        + "m max=" + std::to_string(max_depth_meters_) + "m" : "");
  log("Edge Invalidation", edge_inv_enabled_,
      edge_inv_enabled_ ? "  width=" + std::to_string(edge_inv_width_) : "");
  log("Speckle Removal", speckle_enabled_,
      speckle_enabled_ ? "  size=" + std::to_string(speckle_max_size_) : "");
  log("Domain Transform", dt_enabled_,
      dt_enabled_ ? "  iter=" + std::to_string(dt_iterations_)
        + " a=" + std::to_string(dt_alpha_)
        + " d=" + std::to_string(dt_delta_) : "");
  log("Temporal", temporal_enabled_,
      temporal_enabled_ ? "  alpha=" + std::to_string(temporal_alpha_)
        + " delta=" + std::to_string(temporal_delta_)
        + " persist=" + std::to_string(temporal_persistence_) : "");
}

void StereoDepthEstimator::applyFilterChain(cv::Mat& disparity, cv::Mat& disparity_lowres, FilterTimings& timings)
{
  using clock = std::chrono::steady_clock;
  const cv::Size orig_size = disparity.size();
  const int f = filter_downsample_factor_;

  // Downsample for faster filtering (INTER_AREA averages the pixel block → free noise reduction)
  auto t0 = clock::now();
  if (filter_downsample_enabled_ && f > 1) {
    cv::resize(disparity, disparity,
               cv::Size(orig_size.width / f, orig_size.height / f),
               0, 0, cv::INTER_AREA);
  }
  auto t1 = clock::now();
  timings.downsample_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

  // Run filters in configured order
  for (const auto& name : filter_order_) {
    t0 = clock::now();
    if (name == "median" && median_enabled_) {
      applyMedian(disparity);
      t1 = clock::now();
      timings.median_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "bilateral" && bilateral_enabled_) {
      applyBilateral(disparity);
      t1 = clock::now();
      timings.bilateral_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "hole_filling" && hole_filling_enabled_) {
      fillHoles(disparity);
      t1 = clock::now();
      timings.hole_fill_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "depth_clamp" && depth_clamp_enabled_) {
      const float f_calib = static_cast<float>(focal_length_);
      const float t = static_cast<float>(baseline_);
      clampByDepth(disparity, f_calib, t);
      t1 = clock::now();
      timings.depth_clamp_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "edge_invalidation" && edge_inv_enabled_) {
      invalidateEdges(disparity);
      t1 = clock::now();
      timings.edge_inv_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "speckle" && speckle_enabled_) {
      filterSpeckles(disparity);
      t1 = clock::now();
      timings.speckle_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "domain_transform" && dt_enabled_) {
      applyDomainTransform(disparity);
      t1 = clock::now();
      timings.domain_transform_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "temporal" && temporal_enabled_) {
      applyTemporal(disparity);
      t1 = clock::now();
      timings.temporal_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    }
  }

  // Save low-res filtered result for point cloud before upsampling
  disparity_lowres = disparity.clone();

  // Upsample back to original resolution for disparity publishing / depth map
  t0 = clock::now();
  if (filter_downsample_enabled_ && f > 1) {
    cv::resize(disparity, disparity, orig_size, 0, 0, cv::INTER_LINEAR);
  }
  t1 = clock::now();
  timings.upsample_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
}

void StereoDepthEstimator::applyMedian(cv::Mat& img)
{
  if (median_kernel_size_ <= 5) {
    cv::medianBlur(img, img, median_kernel_size_);
  } else {
    double mn, mx;
    cv::minMaxLoc(img, &mn, &mx);
    if (mx <= 0) return;
    cv::Mat u8;
    img.convertTo(u8, CV_8U, 255.0 / mx);
    cv::medianBlur(u8, u8, median_kernel_size_);
    u8.convertTo(img, CV_32F, mx / 255.0);
  }
}

void StereoDepthEstimator::applyBilateral(cv::Mat& img)
{
  cv::Mat src = img.clone();
  cv::bilateralFilter(src, img, bilateral_diameter_,
                      bilateral_sigma_color_, bilateral_sigma_space_);
}

void StereoDepthEstimator::fillHoles(cv::Mat& img)
{
  const int rows = img.rows, cols = img.cols;
  const int max_r = (hole_fill_radius_ >= 9999) ? std::max(rows, cols) : hole_fill_radius_;

  // Strategy 0: fill from left neighbour only (RealSense mode 0)
  // Strategy 1: farthest from around — pick neighbour with smallest disparity
  //             (= farthest depth). Safest, avoids foreground bleed. (RS default)
  // Strategy 2: nearest from around — pick neighbour with largest disparity
  //             (= nearest depth).
  const int strategy = hole_fill_strategy_;

  // Work on a clone so reads don't see writes from this pass
  cv::Mat src = img.clone();

  for (int y = 0; y < rows; ++y) {
    float* dst_row = img.ptr<float>(y);
    for (int x = 0; x < cols; ++x) {
      if (src.at<float>(y, x) > 0) continue;

      // Gather up to 4 neighbours (left, right, up, down)
      float candidates[4] = {0, 0, 0, 0};
      int count = 0;

      // Left
      for (int d = 1; d <= max_r && x - d >= 0; ++d)
        if (src.at<float>(y, x - d) > 0) { candidates[count++] = src.at<float>(y, x - d); break; }
      // Right
      for (int d = 1; d <= max_r && x + d < cols; ++d)
        if (src.at<float>(y, x + d) > 0) { candidates[count++] = src.at<float>(y, x + d); break; }
      // Up
      for (int d = 1; d <= max_r && y - d >= 0; ++d)
        if (src.at<float>(y - d, x) > 0) { candidates[count++] = src.at<float>(y - d, x); break; }
      // Down
      for (int d = 1; d <= max_r && y + d < rows; ++d)
        if (src.at<float>(y + d, x) > 0) { candidates[count++] = src.at<float>(y + d, x); break; }

      if (count == 0) continue;

      if (strategy == 0) {
        // fill_from_left: use the first candidate found (left has priority)
        dst_row[x] = candidates[0];
      } else if (strategy == 1) {
        // farthest_from_around: smallest disparity = farthest depth
        float best = candidates[0];
        for (int i = 1; i < count; ++i)
          if (candidates[i] < best) best = candidates[i];
        dst_row[x] = best;
      } else {
        // nearest_from_around: largest disparity = nearest depth
        float best = candidates[0];
        for (int i = 1; i < count; ++i)
          if (candidates[i] > best) best = candidates[i];
        dst_row[x] = best;
      }
    }
  }
}

void StereoDepthEstimator::clampByDepth(cv::Mat& img, float f, float t)
{
  const float abs_t = std::abs(t);
  if (f <= 0 || abs_t <= 0) return;
  float max_d = (min_depth_meters_ > 0)
    ? f * abs_t / static_cast<float>(min_depth_meters_)
    : std::numeric_limits<float>::max();
  float min_d = (max_depth_meters_ > 0)
    ? f * abs_t / static_cast<float>(max_depth_meters_)
    : 0.0f;
  int killed_near = 0, killed_far = 0, total_valid = 0;
  float d_min_seen = std::numeric_limits<float>::max(), d_max_seen = 0.0f;
  for (int y = 0; y < img.rows; ++y) {
    float* row = img.ptr<float>(y);
    for (int x = 0; x < img.cols; ++x) {
      float d = row[x];
      if (d <= 0) continue;
      total_valid++;
      if (d < d_min_seen) d_min_seen = d;
      if (d > d_max_seen) d_max_seen = d;
      if (d > max_d) { row[x] = 0.0f; killed_near++; }
      else if (d < min_d) { row[x] = 0.0f; killed_far++; }
    }
  }
  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "depth_clamp: f=%.2f t=%.5f | min_depth=%.3fm -> max_d=%.1f | max_depth=%.1fm -> min_d=%.1f | "
    "disp_range=[%.2f, %.2f] | killed: %d near + %d far / %d valid",
    f, abs_t, min_depth_meters_, max_d, max_depth_meters_, min_d,
    d_min_seen, d_max_seen, killed_near, killed_far, total_valid);
}

void StereoDepthEstimator::invalidateEdges(cv::Mat& img)
{
  double mn, mx;
  cv::minMaxLoc(img, &mn, &mx);
  if (mx <= 0) return;
  cv::Mat u8;
  img.convertTo(u8, CV_8U, 255.0 / mx);
  cv::GaussianBlur(u8, u8, cv::Size(3, 3), 0.8);
  cv::Mat edges;
  cv::Canny(u8, edges, edge_canny_low_, edge_canny_high_);
  if (edge_inv_width_ > 1) {
    int k = edge_inv_width_ | 1;
    cv::Mat kernel = cv::getStructuringElement(cv::MORPH_ELLIPSE, cv::Size(k, k));
    cv::dilate(edges, edges, kernel);
  }
  for (int y = 0; y < img.rows; ++y) {
    float* dr = img.ptr<float>(y);
    const uchar* er = edges.ptr<uchar>(y);
    for (int x = 0; x < img.cols; ++x)
      if (er[x] > 0 && dr[x] > 0) dr[x] = 0.0f;
  }
}

void StereoDepthEstimator::filterSpeckles(cv::Mat& img)
{
  const float scale = 16.0f;
  cv::Mat s16;
  img.convertTo(s16, CV_16SC1, scale);
  cv::filterSpeckles(s16, 0, speckle_max_size_, speckle_max_diff_ * scale);
  s16.convertTo(img, CV_32FC1, 1.0 / scale);
}

void StereoDepthEstimator::applyDomainTransform(cv::Mat& img)
{
  for (int i = 0; i < dt_iterations_; ++i) {
    dtPass(img, true);
    dtPass(img, false);
  }
}

// Fast exp approximation (Schraudolph 1999) — ~4× faster than std::exp,
// accurate to ~0.3% in the range [-4, 0] which is our operating range.
static inline float fast_expf(float x) {
  union { float f; int32_t i; } u;
  u.i = static_cast<int32_t>(12102203.0f * x + 1065353216.0f);
  return u.f;
}

void StereoDepthEstimator::dtPass(cv::Mat& img, bool horizontal)
{
  const float a = static_cast<float>(dt_alpha_);
  const float inv_d = -1.0f / static_cast<float>(dt_delta_);

  if (horizontal) {
    const int rows = img.rows, cols = img.cols;
    for (int y = 0; y < rows; ++y) {
      float* r = img.ptr<float>(y);
      for (int x = 1; x < cols; ++x) {
        if (r[x] <= 0 || r[x - 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x - 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x - 1] * w;
      }
      for (int x = cols - 2; x >= 0; --x) {
        if (r[x] <= 0 || r[x + 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x + 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x + 1] * w;
      }
    }
  } else {
    // Transpose → horizontal pass → transpose back (cache-friendly)
    cv::Mat t;
    cv::transpose(img, t);
    const int rows = t.rows, cols = t.cols;
    for (int y = 0; y < rows; ++y) {
      float* r = t.ptr<float>(y);
      for (int x = 1; x < cols; ++x) {
        if (r[x] <= 0 || r[x - 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x - 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x - 1] * w;
      }
      for (int x = cols - 2; x >= 0; --x) {
        if (r[x] <= 0 || r[x + 1] <= 0) continue;
        float w = a * fast_expf(std::abs(r[x] - r[x + 1]) * inv_d);
        r[x] = r[x] * (1.f - w) + r[x + 1] * w;
      }
    }
    cv::transpose(t, img);
  }
}

void StereoDepthEstimator::applyTemporal(cv::Mat& img)
{
  const int total = img.rows * img.cols;

  // First frame or resolution change: initialise history
  if (prev_disparity_frame_.empty() || prev_disparity_frame_.size() != img.size()) {
    prev_disparity_frame_ = img.clone();
    temporal_history_.assign(total, 0);
    return;
  }

  const float a = static_cast<float>(temporal_alpha_);
  const float delta = static_cast<float>(temporal_delta_);
  const int persist = temporal_persistence_;

  // Pre-compute persistence bitmask LUT (same as RealSense).
  // For each 8-bit history value, decide if the pixel is "credible enough".
  // This is done once per frame (256 entries) — trivial cost.
  std::array<bool, 256> persist_lut;
  persist_lut.fill(false);
  if (persist > 0) {
    for (int i = 0; i < 256; ++i) {
      int b7 = !!(i & 1), b6 = !!(i & 2), b5 = !!(i & 4), b4 = !!(i & 8);
      int b3 = !!(i & 16), b2 = !!(i & 32), b1 = !!(i & 64), b0 = !!(i & 128);
      int sum8 = b0+b1+b2+b3+b4+b5+b6+b7;
      int sum3 = b0+b1+b2;
      int sum4 = b0+b1+b2+b3;
      int sum2 = b0+b1;
      int sum5 = b0+b1+b2+b3+b4;
      switch (persist) {
        case 1: persist_lut[i] = (sum8 >= 8); break;   // valid 8/8
        case 2: persist_lut[i] = (sum3 >= 2); break;   // valid 2/last 3
        case 3: persist_lut[i] = (sum4 >= 2); break;   // valid 2/last 4
        case 4: persist_lut[i] = (sum8 >= 2); break;   // valid 2/8
        case 5: persist_lut[i] = (sum2 >= 1); break;   // valid 1/last 2
        case 6: persist_lut[i] = (sum5 >= 1); break;   // valid 1/last 5
        case 7: persist_lut[i] = (sum8 >= 1); break;   // valid 1/8
        case 8: persist_lut[i] = true; break;           // persist indefinitely
        default: break;
      }
    }
  }

  for (int y = 0; y < img.rows; ++y) {
    float* cur = img.ptr<float>(y);
    const float* prv = prev_disparity_frame_.ptr<float>(y);
    const int row_off = y * img.cols;
    for (int x = 0; x < img.cols; ++x) {
      const int idx = row_off + x;
      const bool cur_valid = (cur[x] > 0);
      const bool prv_valid = (prv[x] > 0);

      // Update per-pixel history: shift left, set LSB to current validity
      uint8_t h = temporal_history_[idx];
      h = static_cast<uint8_t>((h << 1) | (cur_valid ? 1 : 0));
      temporal_history_[idx] = h;

      if (cur_valid && prv_valid) {
        // Delta threshold: if change is too large, reset (don't blend)
        float diff = std::abs(cur[x] - prv[x]);
        if (diff < delta) {
          cur[x] = a * cur[x] + (1.f - a) * prv[x];
        }
        // else: keep current value as-is (edge/motion detected)
      } else if (!cur_valid && prv_valid && persist > 0) {
        // Hole in current frame — use persistence to decide whether to fill
        if (persist_lut[h]) {
          cur[x] = prv[x];
        }
      }
      // cur valid, prv invalid: keep current (nothing to blend with)
      // both invalid: leave as zero
    }
  }
  prev_disparity_frame_ = img.clone();
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
