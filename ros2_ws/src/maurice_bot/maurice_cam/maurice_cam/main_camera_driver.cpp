#include "maurice_cam/main_camera_driver.hpp"
#include <filesystem>

using namespace std::chrono_literals;

namespace maurice_cam
{

MainCameraDriver::MainCameraDriver(const rclcpp::NodeOptions & options)
: Node("main_camera_driver", options)
{
  // Declare parameters with defaults
  this->declare_parameter<std::string>("data_directory", "/home/jetson1/innate-os/data");
  this->declare_parameter<std::string>("camera_symlink", "3D");
  this->declare_parameter<int>("width", 1280);  // Capture at 1280x480 (640x480 per side)
  this->declare_parameter<int>("height", 480);
  this->declare_parameter<int>("publish_left_width", 640);   // Publish left at 640x480
  this->declare_parameter<int>("publish_left_height", 480);
  this->declare_parameter<int>("publish_stereo_width", 1280); // Publish stereo at 1280x480
  this->declare_parameter<int>("publish_stereo_height", 480);
  this->declare_parameter<double>("fps", 30.0);
  this->declare_parameter<std::string>("frame_id", "camera_optical_frame");
  this->declare_parameter<std::string>("right_frame_id", "right_camera_optical_frame");
  this->declare_parameter<int>("jpeg_quality", 80);
  this->declare_parameter<bool>("publish_compressed", true);
  this->declare_parameter<int>("compressed_frame_interval", 3);
  this->declare_parameter<bool>("publish_stereo", false);  // Combined stereo image for legacy compatibility
  this->declare_parameter<int>("exposure", -1);  // -1 means use current value
  this->declare_parameter<int>("gain", -1);      // -1 means use current value
  this->declare_parameter<int>("default_gain", 110);  // Default gain for auto-exposure mode
  
  // Auto exposure parameters
  this->declare_parameter<int>("auto_exposure_mode", 0);
  this->declare_parameter<double>("target_brightness", 128.0);
  this->declare_parameter<double>("ae_kp", 0.8);
  this->declare_parameter<int>("auto_exposure_update_interval", 3);

  // Get parameter values
  data_directory_ = this->get_parameter("data_directory").as_string();
  std::string camera_symlink = this->get_parameter("camera_symlink").as_string();
  capture_width_ = this->get_parameter("width").as_int();
  capture_height_ = this->get_parameter("height").as_int();
  publish_left_width_ = this->get_parameter("publish_left_width").as_int();
  publish_left_height_ = this->get_parameter("publish_left_height").as_int();
  publish_stereo_width_ = this->get_parameter("publish_stereo_width").as_int();
  publish_stereo_height_ = this->get_parameter("publish_stereo_height").as_int();
  fps_ = this->get_parameter("fps").as_double();
  frame_id_ = this->get_parameter("frame_id").as_string();
  right_frame_id_ = this->get_parameter("right_frame_id").as_string();
  jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();
  publish_compressed_ = this->get_parameter("publish_compressed").as_bool();
  compressed_frame_interval_ = this->get_parameter("compressed_frame_interval").as_int();
  publish_stereo_ = this->get_parameter("publish_stereo").as_bool();
  
  // Get V4L2 control parameters
  exposure_setting_ = this->get_parameter("exposure").as_int();
  gain_setting_ = this->get_parameter("gain").as_int();
  default_gain_param_ = this->get_parameter("default_gain").as_int();
  
  // Get auto exposure parameter values
  auto_exposure_mode_ = static_cast<AutoExposureMode>(this->get_parameter("auto_exposure_mode").as_int());
  target_brightness_ = this->get_parameter("target_brightness").as_double();
  ae_kp_ = this->get_parameter("ae_kp").as_double();
  auto_exposure_update_interval_ = this->get_parameter("auto_exposure_update_interval").as_int();

  // Find camera symlink by pattern matching
  std::string camera_pattern = camera_symlink;  // Parameter now contains pattern
  std::string symlink_path;
  std::string v4l_dir = "/dev/v4l/by-id/";
  
  std::vector<std::string> matching_symlinks;
  if (std::filesystem::exists(v4l_dir)) {
    for (const auto& entry : std::filesystem::directory_iterator(v4l_dir)) {
      std::string filename = entry.path().filename().string();
      if (filename.find(camera_pattern) != std::string::npos) {
        matching_symlinks.push_back(entry.path().string());
      }
    }
  }
  
  if (matching_symlinks.empty()) {
    RCLCPP_ERROR(this->get_logger(), "Camera symlink matching pattern '%s' not found in %s", 
                 camera_pattern.c_str(), v4l_dir.c_str());
    throw std::runtime_error("Camera symlink not found");
  }
  
  // Prefer -video-index0 if available (typically the capture device)
  // Otherwise use the first match
  bool found_index0 = false;
  for (const auto& symlink : matching_symlinks) {
    std::string filename = std::filesystem::path(symlink).filename().string();
    if (filename.find("-video-index0") != std::string::npos) {
      symlink_path = symlink;
      found_index0 = true;
      RCLCPP_INFO(this->get_logger(), "Found camera symlink matching pattern '%s': %s (preferred -video-index0)", 
                  camera_pattern.c_str(), filename.c_str());
      break;
    }
  }
  
  if (!found_index0) {
    // Fall back to first match
    symlink_path = matching_symlinks[0];
    std::string filename = std::filesystem::path(symlink_path).filename().string();
    RCLCPP_INFO(this->get_logger(), "Found camera symlink matching pattern '%s': %s (no -video-index0 found, using first match)", 
                camera_pattern.c_str(), filename.c_str());
  }
  
  // Resolve the symlink to get actual device path
  std::string resolved_path = std::filesystem::read_symlink(symlink_path).string();
  
  // Handle relative paths properly
  if (resolved_path.find("/dev/") == 0) {
    // Already absolute path
    camera_device_ = resolved_path;
  } else {
    // Relative path, resolve it properly
    std::filesystem::path symlink_dir = std::filesystem::path(symlink_path).parent_path();
    std::filesystem::path full_path = std::filesystem::canonical(symlink_dir / resolved_path);
    camera_device_ = full_path.string();
  }

  // Calculate left image dimensions (half width for stereo camera at publish resolution)
  // Note: Frame is downscaled in GStreamer pipeline, so left is half of publish_stereo_width
  left_width_ = publish_stereo_width_ / 2;
  left_height_ = publish_stereo_height_;

  RCLCPP_INFO(this->get_logger(), "=== Maurice Main Camera Driver ===");
  RCLCPP_INFO(this->get_logger(), "Camera pattern: %s", camera_pattern.c_str());
  RCLCPP_INFO(this->get_logger(), "Camera symlink: %s", symlink_path.c_str());
  RCLCPP_INFO(this->get_logger(), "Resolved device: %s", camera_device_.c_str());
  RCLCPP_INFO(this->get_logger(), "Capture resolution: %dx%d (full FOV)", capture_width_, capture_height_);
  RCLCPP_INFO(this->get_logger(), "Publish stereo: %dx%d (downscaled in GStreamer)", publish_stereo_width_, publish_stereo_height_);
  RCLCPP_INFO(this->get_logger(), "Publish left: %dx%d (natural split: %dx%d)", publish_left_width_, publish_left_height_, left_width_, left_height_);
  RCLCPP_INFO(this->get_logger(), "FPS: %.1f", fps_);
  RCLCPP_INFO(this->get_logger(), "Frame ID: %s", frame_id_.c_str());
  RCLCPP_INFO(this->get_logger(), "JPEG Quality: %d", jpeg_quality_);

  // Initialize publishers
  left_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
    "/mars/main_camera/left/image_raw",
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  right_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
    "/mars/main_camera/right/image_raw",
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  if (publish_compressed_) {
    compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
      "/mars/main_camera/left/image_raw/compressed",
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
  }

  if (publish_stereo_) {
    stereo_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
      "/mars/main_camera/stereo",
      rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
    );
  }

  RCLCPP_INFO(this->get_logger(), "Publishers created:");
  RCLCPP_INFO(this->get_logger(), "  - /mars/main_camera/left/image_raw (%dx%d)", left_width_, left_height_);
  RCLCPP_INFO(this->get_logger(), "  - /mars/main_camera/right/image_raw (%dx%d)", left_width_, left_height_);
  if (publish_compressed_) {
    RCLCPP_INFO(this->get_logger(), "  - /mars/main_camera/left/image_raw/compressed (%dx%d)", left_width_, left_height_);
  }
  if (publish_stereo_) {
    RCLCPP_INFO(this->get_logger(), "  - /mars/main_camera/stereo (%dx%d)", publish_stereo_width_, publish_stereo_height_);
  }

  // Initialize TurboJPEG encoder
  jpeg_encoder_ = std::make_unique<JpegTurboEncoder>();
  RCLCPP_INFO(this->get_logger(), "TurboJPEG encoder initialized");

  // Create camera_info publishers (always — calibration may arrive later via file watch)
  auto sensor_qos = rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort);
  left_info_pub_  = this->create_publisher<sensor_msgs::msg::CameraInfo>(
    "/mars/main_camera/left/camera_info", sensor_qos);
  right_info_pub_ = this->create_publisher<sensor_msgs::msg::CameraInfo>(
    "/mars/main_camera/right/camera_info", sensor_qos);

  // Load stereo calibration (initial attempt + watch for changes every 3s)
  checkCalibrationFile();
  calib_watch_timer_ = this->create_wall_timer(3s,
    std::bind(&MainCameraDriver::checkCalibrationFile, this));

  // Initialize camera
  if (initializeCamera()) {
    camera_initialized_ = true;
    
    // Initialize frame timing tracking
    frame_timestamps_.clear();
    last_stats_print_ = this->now();
    
    // Start frame processing thread
    frame_thread_running_ = true;
    frame_thread_ = std::thread(&MainCameraDriver::frameProcessingLoop, this);
    
    RCLCPP_INFO(this->get_logger(), "Main camera driver initialized successfully");
  } else {
    RCLCPP_ERROR(this->get_logger(), "Failed to initialize main camera");
    throw std::runtime_error("Main camera initialization failed");
  }
}

MainCameraDriver::~MainCameraDriver()
{
  RCLCPP_INFO(this->get_logger(), "Shutting down main camera driver...");
  
  // Stop frame processing thread
  if (frame_thread_running_) {
    frame_thread_running_ = false;
    if (frame_thread_.joinable()) {
      frame_thread_.join();
    }
  }
  
  // Release camera
  if (cap_.isOpened()) {
    cap_.release();
  }
  
  // Close V4L2 control file descriptor
  if (camera_fd_ != -1) {
    close(camera_fd_);
    camera_fd_ = -1;
  }
  
  RCLCPP_INFO(this->get_logger(), "Main camera driver shutdown complete");
}

bool MainCameraDriver::initializeCamera()
{
  RCLCPP_DEBUG(this->get_logger(), "Initializing main camera...");
  
  // Check if device exists
  if (!std::filesystem::exists(camera_device_)) {
    RCLCPP_ERROR(this->get_logger(), "Camera device not found: %s", camera_device_.c_str());
    return false;
  }

  // Create GStreamer pipeline
  std::string pipeline = createGStreamerPipeline();
  RCLCPP_DEBUG(this->get_logger(), "GStreamer pipeline: %s", pipeline.c_str());

  // Open camera with GStreamer backend
  cap_.open(pipeline, cv::CAP_GSTREAMER);
  
  if (!cap_.isOpened()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open camera with GStreamer");
    return false;
  }

  // Verify camera settings
  int actual_width = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_WIDTH));
  int actual_height = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_HEIGHT));
  double actual_fps = cap_.get(cv::CAP_PROP_FPS);
  
  RCLCPP_DEBUG(this->get_logger(), "Camera opened successfully:");
  RCLCPP_DEBUG(this->get_logger(), "  Actual resolution: %dx%d", actual_width, actual_height);
  RCLCPP_DEBUG(this->get_logger(), "  Actual FPS: %.1f", actual_fps);

  if (actual_width != capture_width_ || actual_height != capture_height_) {
    RCLCPP_WARN(this->get_logger(), 
      "Resolution mismatch! Requested: %dx%d, Got: %dx%d",
      capture_width_, capture_height_, actual_width, actual_height);
  }

  // Initialize V4L2 controls
  if (!initializeV4L2Controls()) {
    RCLCPP_WARN(this->get_logger(), "Failed to initialize V4L2 controls, using default settings");
  } else {
    // Configure V4L2 and initialize AE controller based on mode
    switch (auto_exposure_mode_) {
      case AutoExposureMode::HARDWARE:
        // Camera built-in AE (aperture priority) — no PID, reset gain to default
        if (setV4L2Control(V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_APERTURE_PRIORITY)) {
          RCLCPP_DEBUG(this->get_logger(), "AE mode: HARDWARE (aperture priority)");
          if (setV4L2Control(V4L2_CID_GAIN, default_gain_param_)) {
            current_gain_ = default_gain_param_;
            RCLCPP_DEBUG(this->get_logger(), "Reset gain to default: %d", default_gain_param_);
          }
        }
        break;

      case AutoExposureMode::CUSTOM_PID:
        // Custom PID AE — set V4L2 manual mode, initialize and run PID controller
        if (setV4L2Control(V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_MANUAL)) {
          RCLCPP_DEBUG(this->get_logger(), "AE mode: CUSTOM_PID (manual V4L2 + PID controller)");
        }
        auto_exposure_controller_.initialize(exposure_min_, exposure_max_,
                                             target_brightness_, ae_kp_);
        RCLCPP_DEBUG(this->get_logger(), "Auto exposure controller initialized:");
        RCLCPP_DEBUG(this->get_logger(), "  Target brightness: %.1f", target_brightness_);
        RCLCPP_DEBUG(this->get_logger(), "  Proportional gain: Kp=%.2f", ae_kp_);
        RCLCPP_DEBUG(this->get_logger(), "  Update interval: every %d frames (%.1f Hz)",
                    auto_exposure_update_interval_, fps_ / auto_exposure_update_interval_);
        break;

      case AutoExposureMode::MANUAL:
        // Pure manual — set V4L2 manual mode, no PID controller
        if (setV4L2Control(V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_MANUAL)) {
          RCLCPP_DEBUG(this->get_logger(), "AE mode: MANUAL (pure manual, no PID)");
        }
        break;
    }
    
    if (exposure_setting_ >= 0) {
      if (setV4L2Control(V4L2_CID_EXPOSURE_ABSOLUTE, exposure_setting_)) {
        current_exposure_ = exposure_setting_;
        RCLCPP_DEBUG(this->get_logger(), "Set exposure to %d", exposure_setting_);
      }
    }
    
    if (gain_setting_ >= 0) {
      if (setV4L2Control(V4L2_CID_GAIN, gain_setting_)) {
        current_gain_ = gain_setting_;
        RCLCPP_DEBUG(this->get_logger(), "Set gain to %d", gain_setting_);
      }
    }
  }

  return true;
}

std::string MainCameraDriver::createGStreamerPipeline()
{
  // Use MJPG format for better performance with this camera
  // Pipeline: capture at full resolution, then downscale in GStreamer (hardware accelerated)
  // appsink properties:
  //   max-buffers=1  - Only keep 1 frame in queue (always get newest)
  //   drop=true      - Drop old frames if queue is full (prevents memory buildup)
  //   sync=false     - Don't sync to clock (process as fast as possible)
  
  // For Jetson, use nvv4l2decoder for hardware JPEG decode (NVDEC engine) and
  // nvvidconv for hardware-accelerated scaling and rotation (VIC engine).
  // The two NVIDIA elements communicate via NVMM zero-copy buffers.
  // flip-method=2 rotates 180 degrees, interpolation-method=4 uses Smart interpolation
  // videoconvert handles final BGRx→BGR for OpenCV.
  std::string pipeline = 
    "v4l2src device=" + camera_device_ + " io-mode=2 do-timestamp=true ! "
    "image/jpeg,width=" + std::to_string(capture_width_) + 
    ",height=" + std::to_string(capture_height_) + 
    ",framerate=" + std::to_string(static_cast<int>(fps_)) + "/1 ! "
    "nvv4l2decoder mjpeg=true ! "
    "nvvidconv flip-method=2 interpolation-method=4 ! "
    "video/x-raw,width=" + std::to_string(publish_stereo_width_) + 
    ",height=" + std::to_string(publish_stereo_height_) + ",format=BGRx ! "
    "videoconvert ! video/x-raw,format=BGR ! "
    "appsink max-buffers=1 drop=true sync=false";
  
  // Alternative pipeline using videoscale (fallback if nvvidconv fails):
  // "v4l2src device=" + camera_device_ + " io-mode=2 do-timestamp=true ! "
  // "image/jpeg,width=" + std::to_string(capture_width_) + 
  // ",height=" + std::to_string(capture_height_) + 
  // ",framerate=" + std::to_string(static_cast<int>(fps_)) + "/1 ! "
  // "jpegdec ! "
  // "videoscale ! "
  // "video/x-raw,width=" + std::to_string(publish_stereo_width_) + 
  // ",height=" + std::to_string(publish_stereo_height_) + " ! "
  // "videoconvert ! video/x-raw,format=BGR ! "
  // "appsink max-buffers=1 drop=true sync=false";
  // Note: Rotation would need to be done in OpenCV with this fallback
  
  return pipeline;
}

bool MainCameraDriver::initializeV4L2Controls()
{
  RCLCPP_DEBUG(this->get_logger(), "Initializing V4L2 controls...");
  
  // Open camera device for control access
  camera_fd_ = open(camera_device_.c_str(), O_RDWR);
  if (camera_fd_ == -1) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open camera for V4L2 controls: %s", 
                 strerror(errno));
    return false;
  }
  
  // Get exposure control range
  struct v4l2_queryctrl qctrl;
  qctrl.id = V4L2_CID_EXPOSURE_ABSOLUTE;
  if (ioctl(camera_fd_, VIDIOC_QUERYCTRL, &qctrl) == 0) {
    exposure_min_ = qctrl.minimum;
    exposure_max_ = qctrl.maximum;
    RCLCPP_DEBUG(this->get_logger(), "Exposure range: %d - %d", exposure_min_, exposure_max_);
  } else {
    RCLCPP_WARN(this->get_logger(), "Failed to query exposure control");
    exposure_min_ = 1;
    exposure_max_ = 10000;
  }
  
  // Get gain control range
  qctrl.id = V4L2_CID_GAIN;
  if (ioctl(camera_fd_, VIDIOC_QUERYCTRL, &qctrl) == 0) {
    gain_min_ = qctrl.minimum;
    gain_max_ = qctrl.maximum;
    RCLCPP_DEBUG(this->get_logger(), "Gain range: %d - %d", gain_min_, gain_max_);
  } else {
    RCLCPP_WARN(this->get_logger(), "Failed to query gain control");
    gain_min_ = 0;
    gain_max_ = 255;
  }
  
  // Get current values
  current_exposure_ = getV4L2Control(V4L2_CID_EXPOSURE_ABSOLUTE);
  current_gain_ = getV4L2Control(V4L2_CID_GAIN);
  
  RCLCPP_DEBUG(this->get_logger(), "Current exposure: %d, gain: %d", current_exposure_, current_gain_);
  
  v4l2_controls_initialized_ = true;
  RCLCPP_DEBUG(this->get_logger(), "V4L2 controls initialized successfully");
  
  return true;
}

bool MainCameraDriver::setV4L2Control(int control_id, int value)
{
  if (camera_fd_ == -1) {
    RCLCPP_WARN(this->get_logger(), "Camera file descriptor not open");
    return false;
  }
  
  struct v4l2_control ctrl;
  ctrl.id = control_id;
  ctrl.value = value;
  
  if (ioctl(camera_fd_, VIDIOC_S_CTRL, &ctrl) == -1) {
    RCLCPP_WARN(this->get_logger(), "Failed to set control %d to %d: %s", 
                control_id, value, strerror(errno));
    return false;
  }
  
  return true;
}

int MainCameraDriver::getV4L2Control(int control_id)
{
  if (camera_fd_ == -1) {
    RCLCPP_WARN(this->get_logger(), "Camera file descriptor not open");
    return -1;
  }
  
  struct v4l2_control ctrl;
  ctrl.id = control_id;
  
  if (ioctl(camera_fd_, VIDIOC_G_CTRL, &ctrl) == -1) {
    RCLCPP_WARN(this->get_logger(), "Failed to get control %d: %s", 
                control_id, strerror(errno));
    return -1;
  }
  
  return ctrl.value;
}

void MainCameraDriver::frameProcessingLoop()
{
  RCLCPP_INFO(this->get_logger(), "Frame processing loop started");
  
  cv::Mat frame;
  
  while (frame_thread_running_ && rclcpp::ok()) {
    try {
      // Capture frame
      bool success = cap_.read(frame);
      
      if (!success || frame.empty()) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
          "Failed to capture frame");
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        continue;
      }
      
      // Increment frame counter
      frame_count_++;
      
      // Process and publish frame
      processAndPublishFrame(frame);
      
      // Update statistics and log every 1000 frames (~33 seconds at 30 fps)
      updateFrameStats();
      if (frame_count_ % 1000 == 0) {
        printFrameStats();
      }
      
    } catch (const std::exception& e) {
      RCLCPP_ERROR(this->get_logger(), "Error in frame processing: %s", e.what());
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }
  
  RCLCPP_INFO(this->get_logger(), "Frame processing loop ended");
}

void MainCameraDriver::processAndPublishFrame(const cv::Mat& frame)
{
  auto current_time = this->now();
  
  // Frame is already rotated and downscaled to publish_stereo_width x publish_stereo_height by GStreamer
  // Verify dimensions match expected publish resolution
  if (static_cast<int>(frame.cols) != publish_stereo_width_ || 
      static_cast<int>(frame.rows) != publish_stereo_height_) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Frame size mismatch: got %dx%d, expected %dx%d",
                         frame.cols, frame.rows, publish_stereo_width_, publish_stereo_height_);
    return;
  }
  
  // Apply auto exposure control (frame is already rotated and downscaled)
  if (auto_exposure_mode_ == AutoExposureMode::CUSTOM_PID && v4l2_controls_initialized_) {
    frame_counter_++;
    if (frame_counter_ >= auto_exposure_update_interval_) {
      // Use left half of the already-downscaled frame
      cv::Mat left_roi_for_ae = frame(cv::Rect(0, 0, publish_stereo_width_ / 2, publish_stereo_height_));
      applyAutoExposure(left_roi_for_ae);
      frame_counter_ = 0;
    }
  }
  
  // -------------------------
  // (A1) Stereo msg: Frame is already at publish_stereo_width x publish_stereo_height
  // -------------------------
  auto stereo_msg = std::make_unique<sensor_msgs::msg::Image>();
  stereo_msg->header.stamp = current_time;
  stereo_msg->header.frame_id = frame_id_;
  stereo_msg->height = publish_stereo_height_;
  stereo_msg->width = publish_stereo_width_;
  stereo_msg->encoding = "bgr8";
  stereo_msg->is_bigendian = false;
  stereo_msg->step = publish_stereo_width_ * 3;
  stereo_msg->data.resize(stereo_msg->height * stereo_msg->step);
  
  // Wrap the ROS message data buffer as a cv::Mat view
  cv::Mat stereo_out(
    publish_stereo_height_, publish_stereo_width_, CV_8UC3,
    stereo_msg->data.data(),
    stereo_msg->step
  );
  
  // Copy frame directly (already rotated and downscaled by GStreamer)
  // Ensure frame is BGR format (GStreamer might output RGB)
  if (frame.type() == CV_8UC3) {
    // Check if we need to convert RGB to BGR
    // GStreamer videoconvert typically outputs BGR, but verify
    frame.copyTo(stereo_out);
  } else {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                         "Unexpected frame type: %d, expected CV_8UC3", frame.type());
    return;
  }
  
  // -------------------------
  // (A2) Left ROI view from frame (already at publish resolution)
  // After 180° rotation in GStreamer, original right half appears on the left half.
  // -------------------------
  cv::Rect left_roi(0, 0, left_width_, left_height_);
  cv::Mat left_view = stereo_out(left_roi);  // Use stereo_out which is guaranteed BGR
  
  // -------------------------
  // (A3) Left msg: Use publish_left_width x publish_left_height, scale only if needed
  // -------------------------
  auto left_msg = std::make_unique<sensor_msgs::msg::Image>();
  left_msg->header.stamp = current_time;
  left_msg->header.frame_id = frame_id_;
  left_msg->height = publish_left_height_;
  left_msg->width = publish_left_width_;
  left_msg->encoding = "bgr8";
  left_msg->is_bigendian = false;
  left_msg->step = publish_left_width_ * 3;
  left_msg->data.resize(left_msg->height * left_msg->step);
  
  // Wrap left ROS message buffer as cv::Mat view
  cv::Mat left_out(
    publish_left_height_, publish_left_width_, CV_8UC3,
    left_msg->data.data(),
    left_msg->step
  );
  
  // Only resize if the natural split size doesn't match the requested publish size
  if (left_width_ == publish_left_width_ && left_height_ == publish_left_height_) {
    // No scaling needed, copy directly
    left_view.copyTo(left_out);
  } else {
    // Scale to requested size
    cv::resize(left_view, left_out, cv::Size(publish_left_width_, publish_left_height_), 0, 0, cv::INTER_LINEAR);
  }

  // -------------------------
  // (A4) Right ROI view and msg
  // -------------------------
  cv::Rect right_roi(left_width_, 0, left_width_, left_height_);
  cv::Mat right_view = stereo_out(right_roi);
  
  auto right_msg = std::make_unique<sensor_msgs::msg::Image>();
  right_msg->header.stamp = current_time;
  right_msg->header.frame_id = right_frame_id_;
  right_msg->height = publish_left_height_;
  right_msg->width = publish_left_width_;
  right_msg->encoding = "bgr8";
  right_msg->is_bigendian = false;
  right_msg->step = publish_left_width_ * 3;
  right_msg->data.resize(right_msg->height * right_msg->step);
  
  cv::Mat right_out(
    publish_left_height_, publish_left_width_, CV_8UC3,
    right_msg->data.data(),
    right_msg->step
  );
  
  if (left_width_ == publish_left_width_ && left_height_ == publish_left_height_) {
    right_view.copyTo(right_out);
  } else {
    cv::resize(right_view, right_out, cv::Size(publish_left_width_, publish_left_height_), 0, 0, cv::INTER_LINEAR);
  }
  
  // -------------------------
  // Publish with std::move() for zero-copy intra-process communication
  // Inter-process subscribers (via DDS) still receive serialized copies automatically
  // -------------------------
  if (publish_stereo_) {
    stereo_pub_->publish(std::move(stereo_msg));
  }
  left_pub_->publish(std::move(left_msg));
  right_pub_->publish(std::move(right_msg));

  // Publish camera_info with same timestamp
  {
    std::lock_guard<std::mutex> lock(calib_mutex_);
    if (calibration_loaded_) {
      left_info_msg_.header.stamp = current_time;
      right_info_msg_.header.stamp = current_time;
      left_info_pub_->publish(left_info_msg_);
      right_info_pub_->publish(right_info_msg_);
    }
  }
  
  // Conditionally create and publish compressed image at specified interval
  if (publish_compressed_) {
    compressed_frame_counter_++;
    if (compressed_frame_counter_ >= compressed_frame_interval_) {
      compressed_frame_counter_ = 0;
      
      auto left_compressed_msg = std::make_unique<sensor_msgs::msg::CompressedImage>();
      left_compressed_msg->header.stamp = current_time;
      left_compressed_msg->header.frame_id = frame_id_;
      left_compressed_msg->format = "jpeg";
      
      // Encode from the left view using TurboJPEG
      try {
        if (left_view.type() == CV_8UC3 && left_view.channels() == 3) {
          jpeg_encoder_->encodeBGR(left_view, jpeg_quality_, left_compressed_msg->data);
          compressed_pub_->publish(std::move(left_compressed_msg));
        } else {
          RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                               "Left image format invalid for JPEG encoding: type=%d, channels=%d",
                               left_view.type(), left_view.channels());
        }
      } catch (const std::exception& e) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                             "JPEG turbo encode failed: %s", e.what());
      }
    }
  }
}

void MainCameraDriver::applyAutoExposure(const cv::Mat& frame)
{
  // Calculate new exposure value using PID controller
  int new_exposure = auto_exposure_controller_.calculateExposure(frame, current_exposure_);
  
  // Only update if there's a meaningful change
  if (abs(new_exposure - current_exposure_) > 5) {
    if (setV4L2Control(V4L2_CID_EXPOSURE_ABSOLUTE, new_exposure)) {
      current_exposure_ = new_exposure;
      RCLCPP_INFO(this->get_logger(), "Auto exposure: %d -> %d", current_exposure_, new_exposure);
    }
  }
}

void MainCameraDriver::updateFrameStats()
{
  auto current_time = this->now();
  frame_timestamps_.push_back(current_time);
  
  // Remove timestamps older than 1 second
  auto one_second_ago = current_time - rclcpp::Duration::from_nanoseconds(1000000000);
  while (!frame_timestamps_.empty() && frame_timestamps_.front() < one_second_ago) {
    frame_timestamps_.pop_front();
  }
}

void MainCameraDriver::printFrameStats()
{
  if (frame_timestamps_.size() < 2) {
    return;
  }
  
  // Calculate current framerate
  double current_fps = static_cast<double>(frame_timestamps_.size());
  
  // Calculate frame intervals for jitter analysis
  std::vector<double> intervals;
  for (size_t i = 1; i < frame_timestamps_.size(); ++i) {
    double interval = (frame_timestamps_[i] - frame_timestamps_[i-1]).seconds();
    intervals.push_back(interval);
  }
  
  if (intervals.empty()) {
    return;
  }
  
  // Calculate statistics
  double mean_interval = 0.0;
  for (double interval : intervals) {
    mean_interval += interval;
  }
  mean_interval /= intervals.size();
  
  // Calculate jitter (standard deviation)
  double variance = 0.0;
  for (double interval : intervals) {
    double diff = interval - mean_interval;
    variance += diff * diff;
  }
  variance /= intervals.size();
  double jitter_ms = std::sqrt(variance) * 1000.0;
  
  double expected_interval = 1.0 / fps_;
  double timing_error_ms = (mean_interval - expected_interval) * 1000.0;
  
  RCLCPP_INFO(this->get_logger(), 
    "Frame Stats - FPS: %.1f (target: %.1f) | Jitter: %.1f ms | Error: %.1f ms | Samples: %zu | Exposure: %d | Gain: %d",
    current_fps, fps_, jitter_ms, timing_error_ms, frame_timestamps_.size(), current_exposure_, current_gain_);
}

void MainCameraDriver::checkCalibrationFile()
{
  try {
    // Always load from scratch (re-discovers calibration dir + file)
    auto cal = StereoCalibration::load(data_directory_);

    // Check if anything actually changed (skip rebuild if identical file & mtime)
    auto new_write = std::filesystem::last_write_time(cal->filePath());
    if (calibration_loaded_ && stereo_calib_
        && cal->filePath() == stereo_calib_->filePath()
        && new_write == calib_last_write_) {
      return;
    }

    auto left_info  = cal->buildLeftCameraInfo(frame_id_);
    auto right_info = cal->buildRightCameraInfo(right_frame_id_);

    {
      std::lock_guard<std::mutex> lock(calib_mutex_);
      stereo_calib_   = cal;
      left_info_msg_  = left_info;
      right_info_msg_ = right_info;
      calib_last_write_ = new_write;
      calibration_loaded_ = true;
    }

    RCLCPP_INFO(this->get_logger(), "Stereo calibration (re)loaded: %s",
                cal->filePath().string().c_str());
  } catch (const std::exception& e) {
    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 10000,
      "Calibration file watch: %s", e.what());
    
    // Set uncalibrated camera_info (all zeros) if previously loaded (e.g., file was deleted)
    // Per ROS convention: K[0] == 0.0 indicates an uncalibrated camera
    {
      std::lock_guard<std::mutex> lock(calib_mutex_);
      if (calibration_loaded_) {
        stereo_calib_ = nullptr;
        
        // Create zeroed-out camera_info messages to indicate uncalibrated state
        left_info_msg_ = sensor_msgs::msg::CameraInfo();
        left_info_msg_.header.frame_id = frame_id_;
        left_info_msg_.width = publish_left_width_;
        left_info_msg_.height = publish_left_height_;
        
        right_info_msg_ = sensor_msgs::msg::CameraInfo();
        right_info_msg_.header.frame_id = right_frame_id_;
        right_info_msg_.width = publish_left_width_;
        right_info_msg_.height = publish_left_height_;
        
        // Keep calibration_loaded_ = true so we continue publishing the zeroed messages
        RCLCPP_INFO(this->get_logger(), "Calibration cleared - publishing uncalibrated camera_info (K[0]=0)");
      }
    }
  }
}

// AutoExposureController Implementation
AutoExposureController::AutoExposureController()
: kp_(0.8), target_brightness_(128.0),
  min_exposure_(1), max_exposure_(10000),
  center_weight_(0.7), center_region_size_(0.6)
{
}

void AutoExposureController::initialize(int min_exposure, int max_exposure, 
                                       double target_brightness, double kp)
{
  min_exposure_ = min_exposure;
  max_exposure_ = max_exposure;
  target_brightness_ = target_brightness;
  kp_ = kp;
}

int AutoExposureController::calculateExposure(const cv::Mat& frame, int current_exposure)
{
  // Calculate center-weighted brightness
  double brightness = calculateCenterWeightedBrightness(frame);
  
  // Calculate error (target - current)
  double error = target_brightness_ - brightness;
  
  // Simple proportional control
  double output = kp_ * error;
  
  // Scale output to exposure range (15% of range per brightness unit error)
  double exposure_range = max_exposure_ - min_exposure_;
  double normalized_output = output / 128.0; // Scale by target brightness
  int exposure_adjustment = static_cast<int>(normalized_output * exposure_range * 0.15);
  
  // Calculate new exposure value
  int new_exposure = current_exposure + exposure_adjustment;
  
  // Clamp to limits
  return std::clamp(new_exposure, min_exposure_, max_exposure_);
}

double AutoExposureController::calculateCenterWeightedBrightness(const cv::Mat& frame)
{
  // Convert to grayscale if needed
  cv::Mat gray;
  if (frame.channels() == 3) {
    cv::cvtColor(frame, gray, cv::COLOR_BGR2GRAY);
  } else {
    gray = frame;
  }
  
  // Get image dimensions
  int h = gray.rows;
  int w = gray.cols;
  
  // Define center region
  int center_h = static_cast<int>(h * center_region_size_);
  int center_w = static_cast<int>(w * center_region_size_);
  int start_h = (h - center_h) / 2;
  int start_w = (w - center_w) / 2;
  
  cv::Rect center_roi(start_w, start_h, center_w, center_h);
  
  // Calculate brightness for center region
  cv::Scalar center_mean = cv::mean(gray(center_roi));
  
  // Calculate brightness for entire image
  cv::Scalar full_mean = cv::mean(gray);
  
  // Return weighted average (center gets higher weight)
  return center_weight_ * center_mean[0] + (1.0 - center_weight_) * full_mean[0];
}

void AutoExposureController::setPID(double kp)
{
  kp_ = kp;
}

void AutoExposureController::setTargetBrightness(double target)
{
  target_brightness_ = target;
}

} // namespace maurice_cam

// Register the component
RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::MainCameraDriver)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  
  try {
    auto node = std::make_shared<maurice_cam::MainCameraDriver>();
    rclcpp::spin(node);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(rclcpp::get_logger("main"), "Exception: %s", e.what());
    return 1;
  }
  
  rclcpp::shutdown();
  return 0;
}
#endif

