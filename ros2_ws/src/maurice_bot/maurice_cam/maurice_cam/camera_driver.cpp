#include "maurice_cam/camera_driver.hpp"
#include <filesystem>

using namespace std::chrono_literals;

namespace maurice_cam
{

CameraDriver::CameraDriver()
: Node("camera_driver")
{
  // Declare parameters with defaults
  this->declare_parameter<std::string>("camera_symlink", "usb-3D_USB_Camera_3D_USB_Camera_01.00.00-video-index0");
  this->declare_parameter<int>("width", 1280);
  this->declare_parameter<int>("height", 480);
  this->declare_parameter<double>("fps", 30.0);
  this->declare_parameter<std::string>("frame_id", "camera_optical_frame");
  this->declare_parameter<int>("jpeg_quality", 80);
  this->declare_parameter<int>("exposure", -1);  // -1 means use current value
  this->declare_parameter<int>("gain", -1);      // -1 means use current value
  this->declare_parameter<bool>("disable_auto_exposure", false);
  this->declare_parameter<int>("default_gain", 110);  // Default gain for auto-exposure mode
  
  // Auto exposure parameters
  this->declare_parameter<bool>("enable_auto_exposure", true);
  this->declare_parameter<double>("target_brightness", 128.0);
  this->declare_parameter<double>("ae_kp", 0.8);
  this->declare_parameter<int>("auto_exposure_update_interval", 3);

  // Get parameter values
  std::string camera_symlink = this->get_parameter("camera_symlink").as_string();
  capture_width_ = this->get_parameter("width").as_int();
  capture_height_ = this->get_parameter("height").as_int();
  fps_ = this->get_parameter("fps").as_double();
  frame_id_ = this->get_parameter("frame_id").as_string();
  jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();
  
  // Get V4L2 control parameters
  exposure_setting_ = this->get_parameter("exposure").as_int();
  gain_setting_ = this->get_parameter("gain").as_int();
  disable_auto_exposure_ = this->get_parameter("disable_auto_exposure").as_bool();
  default_gain_param_ = this->get_parameter("default_gain").as_int();
  
  // Get auto exposure parameter values
  enable_auto_exposure_ = this->get_parameter("enable_auto_exposure").as_bool();
  target_brightness_ = this->get_parameter("target_brightness").as_double();
  ae_kp_ = this->get_parameter("ae_kp").as_double();
  auto_exposure_update_interval_ = this->get_parameter("auto_exposure_update_interval").as_int();

  // Resolve symlink to device path
  std::string symlink_path = "/dev/v4l/by-id/" + camera_symlink;
  if (!std::filesystem::exists(symlink_path)) {
    RCLCPP_ERROR(this->get_logger(), "Camera symlink not found: %s", symlink_path.c_str());
    throw std::runtime_error("Camera symlink not found");
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

  // Calculate left image dimensions (half width for stereo camera)
  left_width_ = capture_width_ / 2;
  left_height_ = capture_height_;

  RCLCPP_INFO(this->get_logger(), "=== Maurice Camera Driver ===");
  RCLCPP_INFO(this->get_logger(), "Camera symlink: %s", camera_symlink.c_str());
  RCLCPP_INFO(this->get_logger(), "Resolved device: %s", camera_device_.c_str());
  RCLCPP_INFO(this->get_logger(), "Stereo resolution: %dx%d", capture_width_, capture_height_);
  RCLCPP_INFO(this->get_logger(), "Left camera resolution: %dx%d", left_width_, left_height_);
  RCLCPP_INFO(this->get_logger(), "FPS: %.1f", fps_);
  RCLCPP_INFO(this->get_logger(), "Frame ID: %s", frame_id_.c_str());
  RCLCPP_INFO(this->get_logger(), "JPEG Quality: %d", jpeg_quality_);

  // Initialize publishers
  image_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
    "/color/image",
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
    "/color/image/compressed",
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  stereo_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
    "/color/stereo",
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  RCLCPP_INFO(this->get_logger(), "Publishers created:");
  RCLCPP_INFO(this->get_logger(), "  - /color/image (left camera, rotated)");
  RCLCPP_INFO(this->get_logger(), "  - /color/image/compressed (left camera, rotated)");
  RCLCPP_INFO(this->get_logger(), "  - /color/stereo (full stereo, rotated)");

  // Initialize camera
  if (initializeCamera()) {
    camera_initialized_ = true;
    
    // Initialize frame timing tracking
    frame_timestamps_.clear();
    last_stats_print_ = this->now();
    
    // Start frame processing thread
    frame_thread_running_ = true;
    frame_thread_ = std::thread(&CameraDriver::frameProcessingLoop, this);
    
    RCLCPP_INFO(this->get_logger(), "Camera driver initialized successfully");
  } else {
    RCLCPP_ERROR(this->get_logger(), "Failed to initialize camera");
    throw std::runtime_error("Camera initialization failed");
  }
}

CameraDriver::~CameraDriver()
{
  RCLCPP_INFO(this->get_logger(), "Shutting down camera driver...");
  
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
  
  RCLCPP_INFO(this->get_logger(), "Camera driver shutdown complete");
}

bool CameraDriver::initializeCamera()
{
  RCLCPP_INFO(this->get_logger(), "Initializing camera...");
  
  // Check if device exists
  if (!std::filesystem::exists(camera_device_)) {
    RCLCPP_ERROR(this->get_logger(), "Camera device not found: %s", camera_device_.c_str());
    return false;
  }

  // Create GStreamer pipeline
  std::string pipeline = createGStreamerPipeline();
  RCLCPP_INFO(this->get_logger(), "GStreamer pipeline: %s", pipeline.c_str());

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
  
  RCLCPP_INFO(this->get_logger(), "Camera opened successfully:");
  RCLCPP_INFO(this->get_logger(), "  Actual resolution: %dx%d", actual_width, actual_height);
  RCLCPP_INFO(this->get_logger(), "  Actual FPS: %.1f", actual_fps);

  if (actual_width != capture_width_ || actual_height != capture_height_) {
    RCLCPP_WARN(this->get_logger(), 
      "Resolution mismatch! Requested: %dx%d, Got: %dx%d",
      capture_width_, capture_height_, actual_width, actual_height);
  }

  // Initialize V4L2 controls
  if (!initializeV4L2Controls()) {
    RCLCPP_WARN(this->get_logger(), "Failed to initialize V4L2 controls, using default settings");
  } else {
    // Initialize auto exposure controller if enabled
    if (enable_auto_exposure_ && !disable_auto_exposure_) {
      auto_exposure_controller_.initialize(exposure_min_, exposure_max_, 
                                         target_brightness_, ae_kp_);
      RCLCPP_INFO(this->get_logger(), "Auto exposure controller initialized:");
      RCLCPP_INFO(this->get_logger(), "  Target brightness: %.1f", target_brightness_);
      RCLCPP_INFO(this->get_logger(), "  Proportional gain: Kp=%.2f", ae_kp_);
      RCLCPP_INFO(this->get_logger(), "  Update interval: every %d frames (%.1f Hz)", 
                  auto_exposure_update_interval_, fps_ / auto_exposure_update_interval_);
    }
    // Apply parameter settings if specified
    if (disable_auto_exposure_ || enable_auto_exposure_) {
      // Use manual mode for either manual control or custom auto exposure
      if (setV4L2Control(V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_MANUAL)) {
        if (enable_auto_exposure_) {
          RCLCPP_INFO(this->get_logger(), "Disabled camera auto exposure (Manual Mode for custom AE)");
        } else {
          RCLCPP_INFO(this->get_logger(), "Disabled auto exposure (Manual Mode)");
        }
      }
    } else {
      // Use camera's built-in auto exposure
      if (setV4L2Control(V4L2_CID_EXPOSURE_AUTO, V4L2_EXPOSURE_APERTURE_PRIORITY)) {
        RCLCPP_INFO(this->get_logger(), "Enabled camera auto exposure (Aperture Priority Mode)");
        // Reset gain to default when switching to auto-exposure
        if (setV4L2Control(V4L2_CID_GAIN, default_gain_param_)) {
          current_gain_ = default_gain_param_;
          RCLCPP_INFO(this->get_logger(), "Reset gain to default: %d", default_gain_param_);
        }
      }
    }
    
    if (exposure_setting_ >= 0) {
      if (setV4L2Control(V4L2_CID_EXPOSURE_ABSOLUTE, exposure_setting_)) {
        current_exposure_ = exposure_setting_;
        RCLCPP_INFO(this->get_logger(), "Set exposure to %d", exposure_setting_);
      }
    }
    
    if (gain_setting_ >= 0) {
      if (setV4L2Control(V4L2_CID_GAIN, gain_setting_)) {
        current_gain_ = gain_setting_;
        RCLCPP_INFO(this->get_logger(), "Set gain to %d", gain_setting_);
      }
    }
  }

  return true;
}

std::string CameraDriver::createGStreamerPipeline()
{
  // Use MJPG format for better performance with this camera
  std::string pipeline = 
    "v4l2src device=" + camera_device_ + " ! "
    "image/jpeg,width=" + std::to_string(capture_width_) + 
    ",height=" + std::to_string(capture_height_) + 
    ",framerate=" + std::to_string(static_cast<int>(fps_)) + "/1 ! "
    "jpegdec ! videoconvert ! appsink";
  
  return pipeline;
}

bool CameraDriver::initializeV4L2Controls()
{
  RCLCPP_INFO(this->get_logger(), "Initializing V4L2 controls...");
  
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
    RCLCPP_INFO(this->get_logger(), "Exposure range: %d - %d", exposure_min_, exposure_max_);
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
    RCLCPP_INFO(this->get_logger(), "Gain range: %d - %d", gain_min_, gain_max_);
  } else {
    RCLCPP_WARN(this->get_logger(), "Failed to query gain control");
    gain_min_ = 0;
    gain_max_ = 255;
  }
  
  // Get current values
  current_exposure_ = getV4L2Control(V4L2_CID_EXPOSURE_ABSOLUTE);
  current_gain_ = getV4L2Control(V4L2_CID_GAIN);
  
  RCLCPP_INFO(this->get_logger(), "Current exposure: %d, gain: %d", current_exposure_, current_gain_);
  
  v4l2_controls_initialized_ = true;
  RCLCPP_INFO(this->get_logger(), "V4L2 controls initialized successfully");
  
  return true;
}

bool CameraDriver::setV4L2Control(int control_id, int value)
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

int CameraDriver::getV4L2Control(int control_id)
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

void CameraDriver::frameProcessingLoop()
{
  RCLCPP_INFO(this->get_logger(), "Frame processing loop started");
  
  cv::Mat frame;
  auto last_stats_time = std::chrono::high_resolution_clock::now();
  
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
      
      // Update statistics
      updateFrameStats();
      
      // Print periodic statistics
      auto current_time = std::chrono::high_resolution_clock::now();
      auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
        current_time - last_stats_time).count();
      
      if (elapsed >= 5) {  // Print stats every 5 seconds
        RCLCPP_INFO(this->get_logger(), 
          "Captured %d frames in %ld seconds (avg: %.1f fps)",
          frame_count_.load(), elapsed, 
          static_cast<double>(frame_count_.load()) / elapsed);
        
        frame_count_ = 0;
        last_stats_time = current_time;
      }
      
    } catch (const std::exception& e) {
      RCLCPP_ERROR(this->get_logger(), "Error in frame processing: %s", e.what());
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }
  
  RCLCPP_INFO(this->get_logger(), "Frame processing loop ended");
}

void CameraDriver::processAndPublishFrame(const cv::Mat& frame)
{
  auto current_time = this->now();
  
  // Extract left half of the stereo image (left camera only)
  cv::Mat left_frame = frame(cv::Rect(0, 0, left_width_, left_height_)).clone();
  
  // Apply auto exposure control if enabled
  if (enable_auto_exposure_ && !disable_auto_exposure_ && v4l2_controls_initialized_) {
    frame_counter_++;
    if (frame_counter_ >= auto_exposure_update_interval_) {
      applyAutoExposure(left_frame);
      frame_counter_ = 0;
    }
  }
  
  // Rotate the left image 180 degrees
  cv::rotate(left_frame, left_frame, cv::ROTATE_180);
  
  // Create and publish left camera raw image message
  sensor_msgs::msg::Image left_ros_image;
  left_ros_image.header.stamp = current_time;
  left_ros_image.header.frame_id = frame_id_;
  left_ros_image.height = left_frame.rows;
  left_ros_image.width = left_frame.cols;
  left_ros_image.encoding = "bgr8";
  left_ros_image.is_bigendian = false;
  left_ros_image.step = left_frame.cols * left_frame.channels();
  
  // Copy left image data
  size_t left_data_size = left_frame.total() * left_frame.elemSize();
  left_ros_image.data.resize(left_data_size);
  std::memcpy(left_ros_image.data.data(), left_frame.data, left_data_size);
  
  // Publish left camera raw image
  image_pub_->publish(left_ros_image);
  
  // Create and publish left camera compressed image message
  sensor_msgs::msg::CompressedImage left_compressed_msg;
  left_compressed_msg.header = left_ros_image.header;
  left_compressed_msg.format = "jpeg";
  
  // Compress left image with specified quality
  std::vector<int> params = {
    cv::IMWRITE_JPEG_QUALITY, jpeg_quality_,
    cv::IMWRITE_JPEG_OPTIMIZE, 0
  };
  cv::imencode(".jpg", left_frame, left_compressed_msg.data, params);
  
  // Publish left camera compressed image
  compressed_pub_->publish(left_compressed_msg);
  
  // Create full stereo image (rotated 180 degrees)
  cv::Mat stereo_frame = frame.clone();
  cv::rotate(stereo_frame, stereo_frame, cv::ROTATE_180);
  
  // Create and publish stereo image message
  sensor_msgs::msg::Image stereo_ros_image;
  stereo_ros_image.header.stamp = current_time;
  stereo_ros_image.header.frame_id = frame_id_;
  stereo_ros_image.height = stereo_frame.rows;
  stereo_ros_image.width = stereo_frame.cols;
  stereo_ros_image.encoding = "bgr8";
  stereo_ros_image.is_bigendian = false;
  stereo_ros_image.step = stereo_frame.cols * stereo_frame.channels();
  
  // Copy stereo image data
  size_t stereo_data_size = stereo_frame.total() * stereo_frame.elemSize();
  stereo_ros_image.data.resize(stereo_data_size);
  std::memcpy(stereo_ros_image.data.data(), stereo_frame.data, stereo_data_size);
  
  // Publish stereo image
  stereo_pub_->publish(stereo_ros_image);
}

void CameraDriver::applyAutoExposure(const cv::Mat& frame)
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

void CameraDriver::updateFrameStats()
{
  auto current_time = this->now();
  frame_timestamps_.push_back(current_time);
  
  // Remove timestamps older than 1 second
  auto one_second_ago = current_time - rclcpp::Duration::from_nanoseconds(1000000000);
  while (!frame_timestamps_.empty() && frame_timestamps_.front() < one_second_ago) {
    frame_timestamps_.pop_front();
  }
  
  // Print detailed stats every 10 seconds
  if ((current_time - last_stats_print_).seconds() >= 10.0) {
    printFrameStats();
    last_stats_print_ = current_time;
  }
}

void CameraDriver::printFrameStats()
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

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  
  try {
    auto node = std::make_shared<maurice_cam::CameraDriver>();
    rclcpp::spin(node);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(rclcpp::get_logger("main"), "Exception: %s", e.what());
    return 1;
  }
  
  rclcpp::shutdown();
  return 0;
}
