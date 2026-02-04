#include "maurice_cam/stereo_depth_estimator.hpp"
#include <nlohmann/json.hpp>
#include <fstream>

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
  this->declare_parameter<std::string>("left_rectified_topic", "/mars/main_camera/left/image_rect");
  this->declare_parameter<std::string>("right_rectified_topic", "/mars/main_camera/right/image_rect");
  this->declare_parameter<std::string>("frame_id", "camera_optical_frame");
  this->declare_parameter<int>("image_width", 640);   // Single camera image width
  this->declare_parameter<int>("image_height", 480);  // Single camera image height
  this->declare_parameter<int>("process_every_n_frames", 1);  // 1 = process every frame
  this->declare_parameter<std::string>("pointcloud_topic", "/mars/main_camera/points");
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
  left_rectified_topic_ = this->get_parameter("left_rectified_topic").as_string();
  right_rectified_topic_ = this->get_parameter("right_rectified_topic").as_string();
  frame_id_ = this->get_parameter("frame_id").as_string();
  image_width_ = this->get_parameter("image_width").as_int();
  image_height_ = this->get_parameter("image_height").as_int();
  process_every_n_frames_ = this->get_parameter("process_every_n_frames").as_int();
  if (process_every_n_frames_ < 1) process_every_n_frames_ = 1;
  pointcloud_topic_ = this->get_parameter("pointcloud_topic").as_string();
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
    auto calib_dir = findCalibrationConfigDir();
    auto calib_file = calib_dir / "stereo_calib.yaml";
    
    if (!loadCalibration(calib_file)) {
      throw std::runtime_error("Failed to load calibration from: " + calib_file.string());
    }
    calibration_loaded_ = true;
    RCLCPP_INFO(this->get_logger(), "Loaded calibration from: %s", calib_file.string().c_str());
    RCLCPP_INFO(this->get_logger(), "Calibration resolution: %dx%d", calib_width_, calib_height_);
    RCLCPP_INFO(this->get_logger(), "Depth scale factor: %.2f (input -> calib)", depth_scale_);
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
  disparity_pub_ = this->create_publisher<sensor_msgs::msg::Image>(disparity_topic_, sensor_qos);
  left_rectified_pub_ = this->create_publisher<sensor_msgs::msg::Image>(left_rectified_topic_, sensor_qos);
  right_rectified_pub_ = this->create_publisher<sensor_msgs::msg::Image>(right_rectified_topic_, sensor_qos);
  pointcloud_pub_ = this->create_publisher<sensor_msgs::msg::PointCloud2>(pointcloud_topic_, sensor_qos);
  
  RCLCPP_INFO(this->get_logger(), "Publishers created (lazy publishing - only publish when subscribed)");
  RCLCPP_INFO(this->get_logger(), "  Depth: %s", depth_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Disparity: %s", disparity_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Left rectified: %s", left_rectified_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Right rectified: %s", right_rectified_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Point cloud: %s (decimation: %d)", pointcloud_topic_.c_str(), pointcloud_decimation_);

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

std::filesystem::path StereoDepthEstimator::findCalibrationConfigDir()
{
  std::filesystem::path data_path(data_directory_);
  std::filesystem::path robot_info_path = data_path / "robot_info.json";

  // Try to read robot_info.json to get the robot model
  std::string robot_model;
  if (std::filesystem::exists(robot_info_path)) {
    try {
      std::ifstream file(robot_info_path);
      nlohmann::json robot_info;
      file >> robot_info;
      
      if (robot_info.contains("model")) {
        robot_model = robot_info["model"].get<std::string>();
        RCLCPP_INFO(this->get_logger(), "Found robot model: %s", robot_model.c_str());
      }
    } catch (const std::exception& e) {
      RCLCPP_WARN(this->get_logger(), "Could not parse robot_info.json: %s", e.what());
    }
  }

  // Look for calibration_config_* directories
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (entry.is_directory()) {
      std::string dirname = entry.path().filename().string();
      if (dirname.find("calibration_config") != std::string::npos) {
        // If we have a robot model, prefer matching directory
        if (!robot_model.empty() && dirname.find(robot_model) != std::string::npos) {
          RCLCPP_INFO(this->get_logger(), "Found matching calibration dir: %s", dirname.c_str());
          return entry.path();
        }
        // Otherwise use the first calibration_config directory found
        if (robot_model.empty()) {
          RCLCPP_INFO(this->get_logger(), "Using calibration dir: %s", dirname.c_str());
          return entry.path();
        }
      }
    }
  }

  // If we have a robot model but didn't find exact match, use any calibration_config dir
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (entry.is_directory()) {
      std::string dirname = entry.path().filename().string();
      if (dirname.find("calibration_config") != std::string::npos) {
        RCLCPP_WARN(this->get_logger(), "No exact match for model %s, using: %s", 
                    robot_model.c_str(), dirname.c_str());
        return entry.path();
      }
    }
  }

  throw std::runtime_error("No calibration_config directory found in: " + data_directory_);
}

bool StereoDepthEstimator::loadCalibration(const std::filesystem::path& calib_path)
{
  if (!std::filesystem::exists(calib_path)) {
    RCLCPP_ERROR(this->get_logger(), "Calibration file not found: %s", calib_path.string().c_str());
    return false;
  }

  cv::FileStorage fs(calib_path.string(), cv::FileStorage::READ);
  if (!fs.isOpened()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open calibration file: %s", calib_path.string().c_str());
    return false;
  }

  // Read calibration parameters
  fs["K1"] >> K1_;
  fs["D1"] >> D1_;
  fs["K2"] >> K2_;
  fs["D2"] >> D2_;
  fs["R"] >> R_;
  fs["T"] >> T_;
  fs["R1"] >> R1_;
  fs["R2"] >> R2_;
  fs["P1"] >> P1_;
  fs["P2"] >> P2_;
  fs["Q"] >> Q_;

  // Read calibration image dimensions
  fs["image_width"] >> calib_width_;
  fs["image_height"] >> calib_height_;

  fs.release();

  // Validate required matrices
  if (K1_.empty() || K2_.empty() || R1_.empty() || R2_.empty() || 
      P1_.empty() || P2_.empty() || Q_.empty()) {
    RCLCPP_ERROR(this->get_logger(), "Missing required calibration matrices");
    return false;
  }

  // Calculate depth scale factor (input resolution -> calibration resolution)
  depth_scale_ = static_cast<double>(calib_width_) / static_cast<double>(image_width_);
  
  RCLCPP_INFO(this->get_logger(), "Scale: input %dx%d -> calib %dx%d (scale=%.3f)",
              image_width_, image_height_, calib_width_, calib_height_, depth_scale_);

  // Extract baseline and focal length from Q matrix
  // Q[2,3] = focal length, Q[3,2] = -1/Tx where Tx = baseline
  focal_length_ = Q_.at<double>(2, 3);
  double neg_inv_tx = Q_.at<double>(3, 2);
  if (std::abs(neg_inv_tx) > 1e-6) {
    baseline_ = std::abs(1.0 / neg_inv_tx);
  } else {
    baseline_ = std::abs(T_.at<double>(0, 0));
    RCLCPP_WARN(this->get_logger(), "Q[3,2] near zero, using T vector for baseline");
  }

  RCLCPP_INFO(this->get_logger(), "Stereo parameters: focal=%.2f px, baseline=%.4f m (%.1f mm)",
              focal_length_, baseline_, baseline_ * 1000.0);

  // Compute OpenCV rectification maps at CALIBRATION resolution
  // Maps are computed for unrotated images (calibration coordinate system)
  cv::initUndistortRectifyMap(K1_, D1_, R1_, P1_, 
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_left_, map2_left_);
  cv::initUndistortRectifyMap(K2_, D2_, R2_, P2_,
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_right_, map2_right_);
  RCLCPP_INFO(this->get_logger(), "Rectification maps computed at %dx%d", calib_width_, calib_height_);

  return true;
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
  // ===== STEP 1: Scale to calibration resolution =====
  // Input images are already in the correct orientation (matching calibration)
  cv::Mat left_scaled, right_scaled;
  cv::resize(left_input, left_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_input, right_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);

  // ===== STEP 2: Convert to grayscale (keep color for point cloud) =====
  cv::Mat left_gray, right_gray;
  cv::Mat left_color_for_pc;  // Color image at input resolution for point cloud
  if (left_input.channels() == 3) {
    cv::cvtColor(left_scaled, left_gray, cv::COLOR_BGR2GRAY);
    cv::cvtColor(right_scaled, right_gray, cv::COLOR_BGR2GRAY);
    // Keep color image for point cloud at input resolution
    left_color_for_pc = left_input.clone();
  } else {
    left_gray = left_scaled;
    right_gray = right_scaled;
  }

  // ===== STEP 3: Rectify using OpenCV =====
  cv::Mat left_rect, right_rect;
  cv::remap(left_gray, left_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
  cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);

  // ===== STEP 4: Publish rectified images if subscribed =====
  const bool pub_left_rect = left_rectified_pub_->get_subscription_count() > 0;
  const bool pub_right_rect = right_rectified_pub_->get_subscription_count() > 0;
  
  if (pub_left_rect || pub_right_rect) {
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

  // ===== STEP 5: Wrap rectified images for VPI (CUDA) =====
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

  // ===== STEP 6: Compute stereo disparity using VPI SGM (CUDA) =====
  VPIStereoDisparityEstimatorParams stereo_params;
  vpiInitStereoDisparityEstimatorParams(&stereo_params);
  stereo_params.maxDisparity = max_disparity_;
  stereo_params.confidenceThreshold = confidence_threshold_;
  stereo_params.minDisparity = min_disparity_;
  stereo_params.p1 = p1_;
  stereo_params.p2 = p2_;
  stereo_params.uniqueness = static_cast<float>(uniqueness_);
  // Note: windowSize is ignored on CUDA (uses 9x7 window internally)
  // Note: p2Alpha and numPasses are OFA-only, not used on CUDA

  status = vpiSubmitStereoDisparityEstimator(vpi_stream_, VPI_BACKEND_CUDA, stereo_payload_,
                                              vpi_left_wrap, vpi_right_wrap,
                                              vpi_disparity_, vpi_confidence_, &stereo_params);
  if (status != VPI_SUCCESS) {
    vpiImageDestroy(vpi_left_wrap);
    vpiImageDestroy(vpi_right_wrap);
    RCLCPP_ERROR(this->get_logger(), "Failed to compute stereo disparity");
    return;
  }

  // Synchronize
  vpiStreamSync(vpi_stream_);

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
  
  cv::Mat disparity_float(calib_height_, calib_width_, CV_32FC1);
  for (int y = 0; y < calib_height_; y++) {
    const int16_t* disp_row = disp_ptr + y * disp_pitch;
    float* float_row = disparity_float.ptr<float>(y);
    for (int x = 0; x < calib_width_; x++) {
      // Convert from Q10.5 to float pixels
      float_row[x] = static_cast<float>(disp_row[x]) / DISPARITY_SCALE;
    }
  }

  vpiImageUnlock(vpi_disparity_);

  // Zero out border pixels to eliminate edge artifacts from stereo matching
  if (disparity_border_margin_ > 0) {
    const int margin = disparity_border_margin_;
    // Top and bottom borders
    for (int y = 0; y < margin && y < calib_height_; y++) {
      float* top_row = disparity_float.ptr<float>(y);
      float* bot_row = disparity_float.ptr<float>(calib_height_ - 1 - y);
      for (int x = 0; x < calib_width_; x++) {
        top_row[x] = 0.0f;
        bot_row[x] = 0.0f;
      }
    }
    // Left and right borders (excluding corners already done)
    for (int y = margin; y < calib_height_ - margin; y++) {
      float* row = disparity_float.ptr<float>(y);
      for (int x = 0; x < margin && x < calib_width_; x++) {
        row[x] = 0.0f;
        row[calib_width_ - 1 - x] = 0.0f;
      }
    }
  }

  // Use cv::reprojectImageTo3D like Python script - uses full Q matrix
  cv::Mat points_3d;
  cv::reprojectImageTo3D(disparity_float, points_3d, Q_, true);  // handleMissingValues=true

  // Extract depth (Z coordinate) and convert to uint16 millimeters
  cv::Mat depth_calib(calib_height_, calib_width_, CV_16SC1);
  const float MAX_DEPTH_M = 10.0f;  // Same as Python script
  
  for (int y = 0; y < calib_height_; y++) {
    const cv::Vec3f* pt_row = points_3d.ptr<cv::Vec3f>(y);
    int16_t* depth_row = depth_calib.ptr<int16_t>(y);
    for (int x = 0; x < calib_width_; x++) {
      float z = pt_row[x][2];  // Z coordinate is depth
      if (std::fabs(z) <= MAX_DEPTH_M && std::isfinite(z)) {
        // Convert meters to millimeters
        depth_row[x] = static_cast<int16_t>(std::clamp(z * 1000.0f, -32768.0f, 32767.0f));
      } else {
        depth_row[x] = 0;  // Invalid depth
      }
    }
  }

  // Prepare disparity visualization if subscribed
  const bool pub_disparity = disparity_pub_->get_subscription_count() > 0;
  cv::Mat disp_vis_calib;
  if (pub_disparity) {
    disp_vis_calib = cv::Mat(calib_height_, calib_width_, CV_8UC1);
    float disp_vis_scale = 255.0f / static_cast<float>(max_disparity_);
    for (int y = 0; y < calib_height_; y++) {
      const float* disp_row = disparity_float.ptr<float>(y);
      uint8_t* vis_row = disp_vis_calib.ptr<uint8_t>(y);
      for (int x = 0; x < calib_width_; x++) {
        vis_row[x] = static_cast<uint8_t>(
          std::clamp(disp_row[x] * disp_vis_scale, 0.0f, 255.0f));
      }
    }
  }

  // ===== STEP 7: Upscale depth to input resolution =====
  cv::Mat depth_full;
  cv::resize(depth_calib, depth_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);

  // Check what needs to be published
  const bool pub_depth = depth_pub_->get_subscription_count() > 0;
  const bool pub_pointcloud = pointcloud_pub_->get_subscription_count() > 0;

  // ===== STEP 8: Generate point cloud if subscribed =====
  if (pub_pointcloud) {
    // Scale camera intrinsics from calibration to input resolution
    const float scale_up = 1.0f / static_cast<float>(depth_scale_);
    const float fx = static_cast<float>(P1_.at<double>(0, 0)) * scale_up;
    const float fy = static_cast<float>(P1_.at<double>(1, 1)) * scale_up;
    const float cx = static_cast<float>(P1_.at<double>(0, 2)) * scale_up;
    const float cy = static_cast<float>(P1_.at<double>(1, 2)) * scale_up;

    const int pc_width = image_width_ / pointcloud_decimation_;
    const int pc_height = image_height_ / pointcloud_decimation_;
    const int step = pointcloud_decimation_;

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

    const int16_t* depth_data = reinterpret_cast<const int16_t*>(depth_full.data);
    const bool has_color = !left_color_for_pc.empty() && left_color_for_pc.channels() == 3;

    for (int v = 0; v < pc_height; ++v) {
      for (int u = 0; u < pc_width; ++u, ++iter_x, ++iter_y, ++iter_z, ++iter_rgb) {
        int img_u = u * step;
        int img_v = v * step;
        int16_t depth_mm = depth_data[img_v * image_width_ + img_u];

        if (true) {
          float z = static_cast<float>(depth_mm) * 0.001f;
          *iter_x = (static_cast<float>(img_u) - cx) * z / fx;
          *iter_y = (static_cast<float>(img_v) - cy) * z / fy;
          *iter_z = z;

          // Get color from the color image (BGR format) and pack as float
          if (has_color) {
            const cv::Vec3b& bgr = left_color_for_pc.at<cv::Vec3b>(img_v, img_u);
            // Pack RGB into float (ROS convention: RGB packed as 0x00RRGGBB)
            uint32_t rgb_packed = (static_cast<uint32_t>(bgr[2]) << 16) |  // R
                                  (static_cast<uint32_t>(bgr[1]) << 8) |   // G
                                  (static_cast<uint32_t>(bgr[0]));         // B
            *iter_rgb = *reinterpret_cast<float*>(&rgb_packed);
          } else {
            uint32_t rgb_packed = 0x00808080;  // Gray fallback
            *iter_rgb = *reinterpret_cast<float*>(&rgb_packed);
          }
        } else {
          *iter_x = std::numeric_limits<float>::quiet_NaN();
          *iter_y = std::numeric_limits<float>::quiet_NaN();
          *iter_z = std::numeric_limits<float>::quiet_NaN();
          uint32_t rgb_packed = 0x00000000;
          *iter_rgb = *reinterpret_cast<float*>(&rgb_packed);
        }
      }
    }

    pointcloud_pub_->publish(std::move(cloud_msg));
  }

  // ===== STEP 9: Publish depth if subscribed =====
  if (pub_depth) {
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

  // ===== STEP 10: Publish disparity visualization if subscribed =====
  if (pub_disparity && !disp_vis_calib.empty()) {
    // Upscale disparity visualization to input resolution
    cv::Mat disp_vis_full;
    cv::resize(disp_vis_calib, disp_vis_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);

    auto disp_msg = std::make_unique<sensor_msgs::msg::Image>();
    disp_msg->header.stamp = timestamp;
    disp_msg->header.frame_id = frame_id_;
    disp_msg->height = image_height_;
    disp_msg->width = image_width_;
    disp_msg->encoding = "mono8";
    disp_msg->is_bigendian = false;
    disp_msg->step = image_width_;
    disp_msg->data.resize(disp_msg->height * disp_msg->step);
    memcpy(disp_msg->data.data(), disp_vis_full.data, disp_msg->data.size());
    disparity_pub_->publish(std::move(disp_msg));
  }

  // Cleanup temporary wrapped images
  vpiImageDestroy(vpi_left_wrap);
  vpiImageDestroy(vpi_right_wrap);
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
