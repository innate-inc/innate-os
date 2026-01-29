#include "maurice_cam/stereo_depth_estimator.hpp"
#include <sensor_msgs/msg/camera_info.hpp>
#include <nlohmann/json.hpp>
#include <tf2/LinearMath/Transform.h>
#include <tf2/LinearMath/Quaternion.h>
#include <tf2/LinearMath/Vector3.h>
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
  this->declare_parameter<std::string>("stereo_topic", "/mars/main_camera/stereo");
  this->declare_parameter<std::string>("depth_topic", "/mars/main_camera/depth");
  this->declare_parameter<std::string>("disparity_topic", "/mars/main_camera/disparity");
  this->declare_parameter<std::string>("rectified_topic", "/mars/main_camera/stereo_rectified");
  this->declare_parameter<std::string>("frame_id", "camera_optical_frame");
  this->declare_parameter<int>("max_disparity", 80);
  this->declare_parameter<bool>("publish_disparity", false);
  this->declare_parameter<bool>("publish_rectified", false);
  this->declare_parameter<int>("stereo_width", 1280);  // Input at 1280x480 (640x480 per camera)
  this->declare_parameter<int>("stereo_height", 480);
  this->declare_parameter<int>("process_every_n_frames", 1);  // 1 = process every frame
  this->declare_parameter<bool>("publish_pointcloud", true);
  this->declare_parameter<std::string>("pointcloud_topic", "/mars/main_camera/points");
  this->declare_parameter<std::string>("camera_info_topic", "/mars/main_camera/camera_info");
  this->declare_parameter<int>("pointcloud_decimation", 2);  // 2 = half resolution point cloud
  
  // Filtered pointcloud parameters (in camera optical frame: Z=forward, X=right, Y=down)
  this->declare_parameter<bool>("publish_filtered_pointcloud", true);
  this->declare_parameter<std::string>("filtered_pointcloud_topic", "/mars/main_camera/points_filtered");
  this->declare_parameter<std::string>("base_frame_id", "base_link");
  this->declare_parameter<double>("filter_max_obstacle_height", 0.35);  // Max Y in camera frame
  this->declare_parameter<double>("filter_min_obstacle_height", 0.01);  // Min Y in camera frame
  this->declare_parameter<double>("filter_max_range", 2.5);             // Max Z (depth)
  this->declare_parameter<double>("filter_min_range", 0.01);            // Min Z (depth)
  
  // Block Matching parameters (from Python script that worked best)
  this->declare_parameter<int>("bm_window_size", 11);       // Match Python script
  this->declare_parameter<int>("bm_quality", 8);            // Max quality (0-8)
  this->declare_parameter<int>("bm_conf_threshold", 16000); // Confidence threshold (lower = more details)

  // Get parameter values
  data_directory_ = this->get_parameter("data_directory").as_string();
  stereo_topic_ = this->get_parameter("stereo_topic").as_string();
  depth_topic_ = this->get_parameter("depth_topic").as_string();
  disparity_topic_ = this->get_parameter("disparity_topic").as_string();
  rectified_topic_ = this->get_parameter("rectified_topic").as_string();
  frame_id_ = this->get_parameter("frame_id").as_string();
  max_disparity_ = this->get_parameter("max_disparity").as_int();
  publish_disparity_ = this->get_parameter("publish_disparity").as_bool();
  publish_rectified_ = this->get_parameter("publish_rectified").as_bool();
  stereo_width_ = this->get_parameter("stereo_width").as_int();
  stereo_height_ = this->get_parameter("stereo_height").as_int();
  process_every_n_frames_ = this->get_parameter("process_every_n_frames").as_int();
  if (process_every_n_frames_ < 1) process_every_n_frames_ = 1;
  publish_pointcloud_ = this->get_parameter("publish_pointcloud").as_bool();
  pointcloud_topic_ = this->get_parameter("pointcloud_topic").as_string();
  camera_info_topic_ = this->get_parameter("camera_info_topic").as_string();
  pointcloud_decimation_ = this->get_parameter("pointcloud_decimation").as_int();
  if (pointcloud_decimation_ < 1) pointcloud_decimation_ = 1;
  
  // Get filtered pointcloud parameters
  publish_filtered_pointcloud_ = this->get_parameter("publish_filtered_pointcloud").as_bool();
  filtered_pointcloud_topic_ = this->get_parameter("filtered_pointcloud_topic").as_string();
  base_frame_id_ = this->get_parameter("base_frame_id").as_string();
  filter_max_obstacle_height_ = this->get_parameter("filter_max_obstacle_height").as_double();
  filter_min_obstacle_height_ = this->get_parameter("filter_min_obstacle_height").as_double();
  filter_max_range_ = this->get_parameter("filter_max_range").as_double();
  filter_min_range_ = this->get_parameter("filter_min_range").as_double();
  
  bm_window_size_ = this->get_parameter("bm_window_size").as_int();
  bm_quality_ = this->get_parameter("bm_quality").as_int();
  bm_conf_threshold_ = this->get_parameter("bm_conf_threshold").as_int();

  // Calculate single image dimensions (input resolution)
  image_width_ = stereo_width_ / 2;
  image_height_ = stereo_height_;

  RCLCPP_INFO(this->get_logger(), "=== Maurice Stereo Depth Estimator (Block Matching) ===");
  RCLCPP_INFO(this->get_logger(), "Data directory: %s", data_directory_.c_str());
  RCLCPP_INFO(this->get_logger(), "Stereo topic: %s", stereo_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Depth topic: %s", depth_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Input stereo dimensions: %dx%d", stereo_width_, stereo_height_);
  RCLCPP_INFO(this->get_logger(), "Input single image: %dx%d", image_width_, image_height_);
  RCLCPP_INFO(this->get_logger(), "Max disparity: %d", max_disparity_);
  RCLCPP_INFO(this->get_logger(), "Block Matching: window=%d, quality=%d, confThreshold=%d", 
              bm_window_size_, bm_quality_, bm_conf_threshold_);
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
  RCLCPP_INFO(this->get_logger(), "VPI initialized successfully (Block Matching on CUDA)");

  // Create publishers
  depth_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
    depth_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  if (publish_disparity_) {
    disparity_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
      disparity_topic_,
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
  }

  if (publish_rectified_) {
    rectified_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
      rectified_topic_,
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
    RCLCPP_INFO(this->get_logger(), "Rectified images enabled: %s", rectified_topic_.c_str());
  }

  if (publish_pointcloud_) {
    pointcloud_pub_ = this->create_publisher<sensor_msgs::msg::PointCloud2>(
      pointcloud_topic_,
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
    RCLCPP_INFO(this->get_logger(), "Point cloud enabled: %s (decimation: %d)",
                pointcloud_topic_.c_str(), pointcloud_decimation_);
  }

  // Filtered pointcloud publisher and TF2 setup
  if (publish_filtered_pointcloud_) {
    filtered_pointcloud_pub_ = this->create_publisher<sensor_msgs::msg::PointCloud2>(
      filtered_pointcloud_topic_,
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
    
    // Initialize TF2 for transforming to base_link
    tf_buffer_ = std::make_shared<tf2_ros::Buffer>(this->get_clock());
    tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
    
    RCLCPP_INFO(this->get_logger(), "Filtered point cloud enabled: %s", filtered_pointcloud_topic_.c_str());
    RCLCPP_INFO(this->get_logger(), "  Base frame: %s, height Z: [%.2f, %.2f] m, range XY: [%.2f, %.2f] m",
                base_frame_id_.c_str(), filter_min_obstacle_height_, filter_max_obstacle_height_,
                filter_min_range_, filter_max_range_);
  }

  // Always publish camera info for depth image consumers
  camera_info_pub_ = this->create_publisher<sensor_msgs::msg::CameraInfo>(
    camera_info_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );
  RCLCPP_INFO(this->get_logger(), "Camera info topic: %s", camera_info_topic_.c_str());

  // Create subscription with zero-copy intra-process communication
  stereo_sub_ = this->create_subscription<sensor_msgs::msg::Image>(
    stereo_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort),
    [this](sensor_msgs::msg::Image::UniquePtr msg) {
      this->stereoImageCallback(std::move(msg));
    }
  );

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

  // If no _debug directory found, look for calibration_config_* directories
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

  // Create stereo disparity estimator payload for Block Matching
  VPIStereoDisparityEstimatorCreationParams stereo_params;
  vpiInitStereoDisparityEstimatorCreationParams(&stereo_params);
  stereo_params.maxDisparity = max_disparity_;
  // Block Matching doesn't use diagonal paths (that's SGM)
  stereo_params.includeDiagonals = 0;

  status = vpiCreateStereoDisparityEstimator(VPI_BACKEND_CUDA, calib_width_, calib_height_,
                                              VPI_IMAGE_FORMAT_U8, &stereo_params, &stereo_payload_);
  CHECK_VPI_STATUS(status, "Failed to create stereo disparity estimator");

  RCLCPP_INFO(this->get_logger(), "VPI Block Matching created at %dx%d, maxDisp=%d",
              calib_width_, calib_height_, max_disparity_);
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

void StereoDepthEstimator::stereoImageCallback(sensor_msgs::msg::Image::UniquePtr msg)
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
  if (static_cast<int>(msg->width) != stereo_width_ || 
      static_cast<int>(msg->height) != stereo_height_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Image size mismatch: got %dx%d, expected %dx%d",
                         msg->width, msg->height, stereo_width_, stereo_height_);
    return;
  }

  // Wrap ROS message data as cv::Mat (zero-copy)
  cv::Mat stereo_frame;
  if (msg->encoding == "bgr8") {
    stereo_frame = cv::Mat(msg->height, msg->width, CV_8UC3, msg->data.data(), msg->step);
  } else if (msg->encoding == "rgb8") {
    cv::Mat rgb(msg->height, msg->width, CV_8UC3, msg->data.data(), msg->step);
    cv::cvtColor(rgb, stereo_frame, cv::COLOR_RGB2BGR);
  } else if (msg->encoding == "mono8") {
    stereo_frame = cv::Mat(msg->height, msg->width, CV_8UC1, msg->data.data(), msg->step);
  } else {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Unsupported encoding: %s", msg->encoding.c_str());
    return;
  }

  // Process the frame
  try {
    processFrame(stereo_frame, rclcpp::Time(msg->header.stamp));
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

void StereoDepthEstimator::processFrame(const cv::Mat& stereo_frame, const rclcpp::Time& timestamp)
{
  // ===== STEP 0: Unrotate entire stereo frame to match calibration coordinate system =====
  // Calibration was done on unrotated images, but camera driver rotates 180° (camera mounted upside down).
  // We unrotate for rectification, then rotate back to get the correct view.
  cv::Mat stereo_unrotated;
  cv::rotate(stereo_frame, stereo_unrotated, cv::ROTATE_180);

  // ===== STEP 1: Split stereo frame into left and right images =====
  // After unrotation, first half = original LEFT camera, second half = original RIGHT camera
  cv::Mat left_img = stereo_unrotated(cv::Rect(0, 0, image_width_, image_height_));
  cv::Mat right_img = stereo_unrotated(cv::Rect(image_width_, 0, image_width_, image_height_));

  // ===== STEP 2: Downscale to calibration resolution =====
  cv::Mat left_scaled, right_scaled;
  cv::resize(left_img, left_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_img, right_scaled, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);

  // ===== STEP 3: Convert to grayscale =====
  cv::Mat left_gray, right_gray;
  if (stereo_frame.channels() == 3) {
    cv::cvtColor(left_scaled, left_gray, cv::COLOR_BGR2GRAY);
    cv::cvtColor(right_scaled, right_gray, cv::COLOR_BGR2GRAY);
  } else {
    left_gray = left_scaled;
    right_gray = right_scaled;
  }

  // ===== STEP 4: Rectify using OpenCV =====
  // After unrotation, left_img is the actual LEFT camera, right_img is the actual RIGHT camera
  // Use map1_left_ for left and map1_right_ for right
  // NOTE: Keep rectified images in calibration coordinate system for stereo matching
  // (epipolar lines must be horizontal). We'll rotate the final depth output instead.
  cv::Mat left_rect, right_rect;
  cv::remap(left_gray, left_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
  cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);

  // ===== STEP 4b: Rectify color image for point cloud (if input is color) =====
  cv::Mat left_color_rect;
  if (stereo_frame.channels() == 3 && publish_pointcloud_) {
    // Rectify the color image using the same maps
    cv::remap(left_scaled, left_color_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
  }

  // Publish rectified left image if enabled (before wrapping for VPI)
  // Left image overlaps with the depth output
  // Rotate it back to correct view for publishing
  if (publish_rectified_ && rectified_pub_) {
    // Upscale left rectified image to input resolution
    cv::Mat left_rect_full;
    cv::resize(left_rect, left_rect_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
    
    // Rotate back to correct view for publishing
    cv::rotate(left_rect_full, left_rect_full, cv::ROTATE_180);
    
    auto rectified_msg = std::make_unique<sensor_msgs::msg::Image>();
    rectified_msg->header.stamp = timestamp;
    rectified_msg->header.frame_id = frame_id_;
    rectified_msg->height = image_height_;
    rectified_msg->width = image_width_;
    rectified_msg->encoding = "mono8";
    rectified_msg->is_bigendian = false;
    rectified_msg->step = image_width_;
    rectified_msg->data.resize(rectified_msg->height * rectified_msg->step);
    memcpy(rectified_msg->data.data(), left_rect_full.data, rectified_msg->data.size());
    
    rectified_pub_->publish(std::move(rectified_msg));
  }

  // ===== STEP 5: Wrap rectified images for VPI =====
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

  // ===== STEP 6: Compute stereo disparity using VPI Block Matching =====
  VPIStereoDisparityEstimatorParams stereo_params;
  vpiInitStereoDisparityEstimatorParams(&stereo_params);
  stereo_params.maxDisparity = max_disparity_;
  stereo_params.windowSize = bm_window_size_;
  stereo_params.quality = bm_quality_;
  stereo_params.confidenceThreshold = bm_conf_threshold_;

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

  // Use cv::reprojectImageTo3D like Python script - uses full Q matrix
  cv::Mat points_3d;
  cv::reprojectImageTo3D(disparity_float, points_3d, Q_, true);  // handleMissingValues=true

  // Extract depth (Z coordinate) and convert to uint16 millimeters
  cv::Mat depth_calib(calib_height_, calib_width_, CV_16UC1);
  const float MAX_DEPTH_M = 10.0f;  // Same as Python script
  const float MIN_DEPTH_M = 0.05f; // Minimum valid depth (5cm)
  
  for (int y = 0; y < calib_height_; y++) {
    const cv::Vec3f* pt_row = points_3d.ptr<cv::Vec3f>(y);
    uint16_t* depth_row = depth_calib.ptr<uint16_t>(y);
    for (int x = 0; x < calib_width_; x++) {
      // Z can be negative depending on Q matrix sign convention - use absolute value
      float z = std::abs(pt_row[x][2]);
      if (z >= MIN_DEPTH_M && z <= MAX_DEPTH_M && std::isfinite(z)) {
        // Convert meters to millimeters
        depth_row[x] = static_cast<uint16_t>(std::clamp(z * 1000.0f, 1.0f, 65535.0f));
      } else {
        depth_row[x] = 0;  // Invalid depth
      }
    }
  }

  // Prepare disparity visualization if needed
  cv::Mat disp_vis_calib;
  if (publish_disparity_ && disparity_pub_) {
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

  // ===== STEP 8: Upscale depth to input resolution =====
  cv::Mat depth_full;
  cv::resize(depth_calib, depth_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);
  
  // Rotate depth image back 180° to get the correct view (camera is mounted upside down)
  cv::rotate(depth_full, depth_full, cv::ROTATE_180);

  // ===== STEP 9: Create and publish depth message =====
  auto depth_msg = std::make_unique<sensor_msgs::msg::Image>();
  depth_msg->header.stamp = timestamp;
  depth_msg->header.frame_id = frame_id_;
  depth_msg->height = image_height_;
  depth_msg->width = image_width_;
  depth_msg->encoding = "16UC1";
  depth_msg->is_bigendian = false;
  depth_msg->step = image_width_ * sizeof(uint16_t);
  depth_msg->data.resize(depth_msg->height * depth_msg->step);
  memcpy(depth_msg->data.data(), depth_full.data, depth_msg->data.size());

  // Generate point cloud if enabled (before moving depth_msg)
  if (publish_pointcloud_ && pointcloud_pub_) {
    // Scale camera intrinsics from calibration to input resolution
    const float scale_up = 1.0f / static_cast<float>(depth_scale_);
    const float fx = static_cast<float>(P1_.at<double>(0, 0)) * scale_up;
    const float fy = static_cast<float>(P1_.at<double>(1, 1)) * scale_up;
    const float cx = static_cast<float>(P1_.at<double>(0, 2)) * scale_up;
    const float cy = static_cast<float>(P1_.at<double>(1, 2)) * scale_up;

    const int pc_width = image_width_ / pointcloud_decimation_;
    const int pc_height = image_height_ / pointcloud_decimation_;
    const int step = pointcloud_decimation_;

    // Prepare color image at output resolution if available
    cv::Mat color_for_pc;
    bool has_color = !left_color_rect.empty();
    if (has_color) {
      // Upscale rectified color to input resolution and rotate to match depth
      cv::resize(left_color_rect, color_for_pc, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
      cv::rotate(color_for_pc, color_for_pc, cv::ROTATE_180);
    }

    auto cloud_msg = std::make_unique<sensor_msgs::msg::PointCloud2>();
    cloud_msg->header.stamp = timestamp;
    cloud_msg->header.frame_id = frame_id_;
    cloud_msg->height = pc_height;
    cloud_msg->width = pc_width;
    cloud_msg->is_dense = false;
    cloud_msg->is_bigendian = false;

    sensor_msgs::PointCloud2Modifier modifier(*cloud_msg);
    if (has_color) {
      modifier.setPointCloud2FieldsByString(2, "xyz", "rgb");
    } else {
      modifier.setPointCloud2FieldsByString(1, "xyz");
    }
    modifier.resize(pc_width * pc_height);

    sensor_msgs::PointCloud2Iterator<float> iter_x(*cloud_msg, "x");
    sensor_msgs::PointCloud2Iterator<float> iter_y(*cloud_msg, "y");
    sensor_msgs::PointCloud2Iterator<float> iter_z(*cloud_msg, "z");

    const uint16_t* depth_data = reinterpret_cast<const uint16_t*>(depth_full.data);

    if (has_color) {
      sensor_msgs::PointCloud2Iterator<uint8_t> iter_rgb(*cloud_msg, "rgb");
      
      for (int v = 0; v < pc_height; ++v) {
        for (int u = 0; u < pc_width; ++u, ++iter_x, ++iter_y, ++iter_z, ++iter_rgb) {
          int img_u = u * step;
          int img_v = v * step;
          uint16_t depth_mm = depth_data[img_v * image_width_ + img_u];

          if (depth_mm > 0) {
            float z = static_cast<float>(depth_mm) * 0.001f;
            *iter_x = (static_cast<float>(img_u) - cx) * z / fx;
            *iter_y = (static_cast<float>(img_v) - cy) * z / fy;
            *iter_z = z;
            
            // Get BGR color from rectified color image
            const cv::Vec3b& bgr = color_for_pc.at<cv::Vec3b>(img_v, img_u);
            // Pack as RGB (ROS convention: R in lowest byte when viewed as uint32)
            iter_rgb[0] = bgr[2];  // R
            iter_rgb[1] = bgr[1];  // G
            iter_rgb[2] = bgr[0];  // B
            iter_rgb[3] = 255;     // A (unused but good practice)
          } else {
            *iter_x = std::numeric_limits<float>::quiet_NaN();
            *iter_y = std::numeric_limits<float>::quiet_NaN();
            *iter_z = std::numeric_limits<float>::quiet_NaN();
            iter_rgb[0] = 0;
            iter_rgb[1] = 0;
            iter_rgb[2] = 0;
            iter_rgb[3] = 0;
          }
        }
      }
    } else {
      // No color available - XYZ only
      for (int v = 0; v < pc_height; ++v) {
        for (int u = 0; u < pc_width; ++u, ++iter_x, ++iter_y, ++iter_z) {
          int img_u = u * step;
          int img_v = v * step;
          uint16_t depth_mm = depth_data[img_v * image_width_ + img_u];

          if (depth_mm > 0) {
            float z = static_cast<float>(depth_mm) * 0.001f;
            *iter_x = (static_cast<float>(img_u) - cx) * z / fx;
            *iter_y = (static_cast<float>(img_v) - cy) * z / fy;
            *iter_z = z;
          } else {
            *iter_x = std::numeric_limits<float>::quiet_NaN();
            *iter_y = std::numeric_limits<float>::quiet_NaN();
            *iter_z = std::numeric_limits<float>::quiet_NaN();
          }
        }
      }
    }

    pointcloud_pub_->publish(std::move(cloud_msg));
  }

  // Generate filtered point cloud if enabled
  // Transform to base_link frame (Z=up, X=forward, Y=left) and filter there
  if (publish_filtered_pointcloud_ && filtered_pointcloud_pub_ && tf_buffer_) {
    // Try to get transform from camera frame to base_link
    geometry_msgs::msg::TransformStamped transform;
    bool have_transform = false;
    
    try {
      transform = tf_buffer_->lookupTransform(
        base_frame_id_, frame_id_, timestamp, rclcpp::Duration::from_seconds(0.1));
      have_transform = true;
    } catch (const tf2::TransformException& ex) {
      RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                           "Could not get transform from %s to %s: %s",
                           frame_id_.c_str(), base_frame_id_.c_str(), ex.what());
    }

    if (have_transform) {
      // Scale camera intrinsics from calibration to input resolution
      const float scale_up = 1.0f / static_cast<float>(depth_scale_);
      const float fx = static_cast<float>(P1_.at<double>(0, 0)) * scale_up;
      const float fy = static_cast<float>(P1_.at<double>(1, 1)) * scale_up;
      const float cx = static_cast<float>(P1_.at<double>(0, 2)) * scale_up;
      const float cy = static_cast<float>(P1_.at<double>(1, 2)) * scale_up;

      const int pc_width = image_width_ / pointcloud_decimation_;
      const int pc_height = image_height_ / pointcloud_decimation_;
      const int step = pointcloud_decimation_;

      // Build tf2 transform from the lookup result
      tf2::Quaternion tf_quat(
        transform.transform.rotation.x,
        transform.transform.rotation.y,
        transform.transform.rotation.z,
        transform.transform.rotation.w
      );
      tf2::Vector3 tf_trans(
        transform.transform.translation.x,
        transform.transform.translation.y,
        transform.transform.translation.z
      );
      tf2::Transform tf_camera_to_base(tf_quat, tf_trans);

      // Prepare color image if available
      cv::Mat color_for_filtered_pc;
      bool has_color = !left_color_rect.empty();
      if (has_color) {
        cv::resize(left_color_rect, color_for_filtered_pc, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
        cv::rotate(color_for_filtered_pc, color_for_filtered_pc, cv::ROTATE_180);
      }

      // Collect filtered points (stored in base_link frame)
      const uint16_t* depth_data = reinterpret_cast<const uint16_t*>(depth_full.data);
      std::vector<std::tuple<float, float, float, uint8_t, uint8_t, uint8_t>> filtered_points;
      filtered_points.reserve(pc_width * pc_height / 4);  // Estimate ~25% valid

      int total_valid_points = 0;
      int filtered_by_range = 0;
      int filtered_by_height = 0;

      for (int v = 0; v < pc_height; ++v) {
        for (int u = 0; u < pc_width; ++u) {
          int img_u = u * step;
          int img_v = v * step;
          uint16_t depth_mm = depth_data[img_v * image_width_ + img_u];

          if (depth_mm > 0) {
            total_valid_points++;
            
            // Compute point in camera optical frame
            float z_cam = static_cast<float>(depth_mm) * 0.001f;
            float x_cam = (static_cast<float>(img_u) - cx) * z_cam / fx;
            float y_cam = (static_cast<float>(img_v) - cy) * z_cam / fy;

            // Transform to base_link frame using tf2
            tf2::Vector3 pt_cam(x_cam, y_cam, z_cam);
            tf2::Vector3 pt_base = tf_camera_to_base * pt_cam;
            float x_base = static_cast<float>(pt_base.x());
            float y_base = static_cast<float>(pt_base.y());
            float z_base = static_cast<float>(pt_base.z());

            // Filter by height (Z in base_link = height above ground)
            if (z_base < filter_min_obstacle_height_ || z_base > filter_max_obstacle_height_) {
              filtered_by_height++;
              continue;
            }

            // Filter by horizontal range (XY distance in base_link frame)
            float horiz_range = std::sqrt(x_base * x_base + y_base * y_base);
            if (horiz_range < filter_min_range_ || horiz_range > filter_max_range_) {
              filtered_by_range++;
              continue;
            }

            // Point passes all filters - store it in base_link frame
            uint8_t r = 0, g = 0, b = 0;
            if (has_color) {
              const cv::Vec3b& bgr = color_for_filtered_pc.at<cv::Vec3b>(img_v, img_u);
              r = bgr[2]; g = bgr[1]; b = bgr[0];
            }
            filtered_points.emplace_back(x_base, y_base, z_base, r, g, b);
          }
        }
      }

      // Log filter statistics periodically
      if (frame_count_ % 100 == 0 && total_valid_points > 0) {
        int kept = static_cast<int>(filtered_points.size());
        float pct_kept = 100.0f * kept / total_valid_points;
        float pct_range = 100.0f * filtered_by_range / total_valid_points;
        float pct_height = 100.0f * filtered_by_height / total_valid_points;
        RCLCPP_INFO(this->get_logger(), 
                    "Pointcloud filter: %d/%d kept (%.1f%%), filtered out: range=%.1f%%, height=%.1f%%",
                    kept, total_valid_points, pct_kept, pct_range, pct_height);
      }

      // Create filtered pointcloud message - publish in base_link frame
      auto filtered_cloud_msg = std::make_unique<sensor_msgs::msg::PointCloud2>();
      filtered_cloud_msg->header.stamp = timestamp;
      filtered_cloud_msg->header.frame_id = base_frame_id_;  // Publish in base_link frame
      filtered_cloud_msg->height = 1;  // Unorganized cloud
      filtered_cloud_msg->width = static_cast<uint32_t>(filtered_points.size());
      filtered_cloud_msg->is_dense = true;
      filtered_cloud_msg->is_bigendian = false;

      sensor_msgs::PointCloud2Modifier filtered_modifier(*filtered_cloud_msg);
      if (has_color) {
        filtered_modifier.setPointCloud2FieldsByString(2, "xyz", "rgb");
      } else {
        filtered_modifier.setPointCloud2FieldsByString(1, "xyz");
      }
      filtered_modifier.resize(filtered_points.size());

      if (!filtered_points.empty()) {
        sensor_msgs::PointCloud2Iterator<float> iter_x(*filtered_cloud_msg, "x");
        sensor_msgs::PointCloud2Iterator<float> iter_y(*filtered_cloud_msg, "y");
        sensor_msgs::PointCloud2Iterator<float> iter_z(*filtered_cloud_msg, "z");

        if (has_color) {
          sensor_msgs::PointCloud2Iterator<uint8_t> iter_rgb(*filtered_cloud_msg, "rgb");
          for (const auto& pt : filtered_points) {
            *iter_x = std::get<0>(pt); ++iter_x;
            *iter_y = std::get<1>(pt); ++iter_y;
            *iter_z = std::get<2>(pt); ++iter_z;
            iter_rgb[0] = std::get<3>(pt);  // R
            iter_rgb[1] = std::get<4>(pt);  // G
            iter_rgb[2] = std::get<5>(pt);  // B
            iter_rgb[3] = 255;              // A
            ++iter_rgb;
          }
        } else {
          for (const auto& pt : filtered_points) {
            *iter_x = std::get<0>(pt); ++iter_x;
            *iter_y = std::get<1>(pt); ++iter_y;
            *iter_z = std::get<2>(pt); ++iter_z;
          }
        }
      }

      filtered_pointcloud_pub_->publish(std::move(filtered_cloud_msg));
    }
  }

  // Publish depth
  depth_pub_->publish(std::move(depth_msg));

  // Publish camera info (scaled to output resolution)
  if (camera_info_pub_) {
    auto info_msg = std::make_unique<sensor_msgs::msg::CameraInfo>();
    info_msg->header.stamp = timestamp;
    info_msg->header.frame_id = frame_id_;
    info_msg->height = image_height_;
    info_msg->width = image_width_;
    info_msg->distortion_model = "plumb_bob";
    
    // Scale intrinsics from calibration to output resolution
    const double scale_up = 1.0 / depth_scale_;
    
    // D - distortion coefficients (use zeros since image is rectified)
    info_msg->d = {0.0, 0.0, 0.0, 0.0, 0.0};
    
    // K - intrinsic matrix (scaled)
    info_msg->k = {
      P1_.at<double>(0, 0) * scale_up, 0.0, P1_.at<double>(0, 2) * scale_up,
      0.0, P1_.at<double>(1, 1) * scale_up, P1_.at<double>(1, 2) * scale_up,
      0.0, 0.0, 1.0
    };
    
    // R - rectification matrix (identity for rectified image)
    info_msg->r = {1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0};
    
    // P - projection matrix (scaled)
    info_msg->p = {
      P1_.at<double>(0, 0) * scale_up, 0.0, P1_.at<double>(0, 2) * scale_up, 0.0,
      0.0, P1_.at<double>(1, 1) * scale_up, P1_.at<double>(1, 2) * scale_up, 0.0,
      0.0, 0.0, 1.0, 0.0
    };
    
    info_msg->binning_x = 0;
    info_msg->binning_y = 0;
    
    camera_info_pub_->publish(std::move(info_msg));
  }

  // Publish CameraInfo
  if (camera_info_pub_) {
    auto cam_info_msg = std::make_unique<sensor_msgs::msg::CameraInfo>();
    cam_info_msg->header.stamp = timestamp;
    cam_info_msg->header.frame_id = frame_id_;
    cam_info_msg->height = image_height_;
    cam_info_msg->width = image_width_;
    cam_info_msg->distortion_model = "plumb_bob";
    
    // Scale intrinsics from calibration to output resolution
    const double scale_up = 1.0 / depth_scale_;
    
    // D - distortion coefficients (use zeros for rectified image)
    cam_info_msg->d = {0.0, 0.0, 0.0, 0.0, 0.0};
    
    // K - intrinsic matrix (scaled to output resolution)
    cam_info_msg->k[0] = P1_.at<double>(0, 0) * scale_up;  // fx
    cam_info_msg->k[1] = 0.0;
    cam_info_msg->k[2] = P1_.at<double>(0, 2) * scale_up;  // cx
    cam_info_msg->k[3] = 0.0;
    cam_info_msg->k[4] = P1_.at<double>(1, 1) * scale_up;  // fy
    cam_info_msg->k[5] = P1_.at<double>(1, 2) * scale_up;  // cy
    cam_info_msg->k[6] = 0.0;
    cam_info_msg->k[7] = 0.0;
    cam_info_msg->k[8] = 1.0;
    
    // R - rectification matrix (identity for rectified image)
    cam_info_msg->r[0] = 1.0; cam_info_msg->r[1] = 0.0; cam_info_msg->r[2] = 0.0;
    cam_info_msg->r[3] = 0.0; cam_info_msg->r[4] = 1.0; cam_info_msg->r[5] = 0.0;
    cam_info_msg->r[6] = 0.0; cam_info_msg->r[7] = 0.0; cam_info_msg->r[8] = 1.0;
    
    // P - projection matrix (scaled to output resolution)
    cam_info_msg->p[0] = P1_.at<double>(0, 0) * scale_up;   // fx'
    cam_info_msg->p[1] = 0.0;
    cam_info_msg->p[2] = P1_.at<double>(0, 2) * scale_up;   // cx'
    cam_info_msg->p[3] = 0.0;  // Tx (0 for left camera)
    cam_info_msg->p[4] = 0.0;
    cam_info_msg->p[5] = P1_.at<double>(1, 1) * scale_up;   // fy'
    cam_info_msg->p[6] = P1_.at<double>(1, 2) * scale_up;   // cy'
    cam_info_msg->p[7] = 0.0;  // Ty
    cam_info_msg->p[8] = 0.0;
    cam_info_msg->p[9] = 0.0;
    cam_info_msg->p[10] = 1.0;
    cam_info_msg->p[11] = 0.0;
    
    cam_info_msg->binning_x = 0;
    cam_info_msg->binning_y = 0;
    
    camera_info_pub_->publish(std::move(cam_info_msg));
  }

  // Publish disparity visualization if enabled
  if (publish_disparity_ && disparity_pub_ && !disp_vis_calib.empty()) {
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
