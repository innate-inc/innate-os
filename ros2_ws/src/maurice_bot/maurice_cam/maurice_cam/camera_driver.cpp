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

  // Get parameter values
  std::string camera_symlink = this->get_parameter("camera_symlink").as_string();
  capture_width_ = this->get_parameter("width").as_int();
  capture_height_ = this->get_parameter("height").as_int();
  fps_ = this->get_parameter("fps").as_double();
  frame_id_ = this->get_parameter("frame_id").as_string();
  jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();

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
    "Frame Stats - FPS: %.1f (target: %.1f) | Jitter: %.1f ms | Error: %.1f ms | Samples: %zu",
    current_fps, fps_, jitter_ms, timing_error_ms, frame_timestamps_.size());
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
