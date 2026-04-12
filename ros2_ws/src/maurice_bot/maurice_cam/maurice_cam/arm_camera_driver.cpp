#include "maurice_cam/arm_camera_driver.hpp"
#include <filesystem>

using namespace std::chrono_literals;

namespace maurice_cam
{

ArmCameraDriver::ArmCameraDriver(const rclcpp::NodeOptions & options) 
  : Node("arm_camera_driver", options)
{
    RCLCPP_INFO(this->get_logger(), "Initializing arm camera driver...");

    // Parameters
    this->declare_parameter<std::string>("camera_symlink", "Arducam");
    this->declare_parameter<int>("width", 640);
    this->declare_parameter<int>("height", 480);
    this->declare_parameter<double>("fps", 30.0);
    this->declare_parameter<std::string>("pixel_format", "YUYV");
    this->declare_parameter<bool>("publish_compressed", false);
    this->declare_parameter<int>("compressed_frame_interval", 5);

    // Get parameters
    std::string camera_symlink = this->get_parameter("camera_symlink").as_string();
    width_ = this->get_parameter("width").as_int();
    height_ = this->get_parameter("height").as_int();
    fps_ = this->get_parameter("fps").as_double();
    publish_compressed_ = this->get_parameter("publish_compressed").as_bool();
    compressed_frame_interval_ = this->get_parameter("compressed_frame_interval").as_int();

    // Find camera symlink by pattern matching
    std::string camera_pattern = camera_symlink;
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
        symlink_path = matching_symlinks[0];
        std::string filename = std::filesystem::path(symlink_path).filename().string();
        RCLCPP_INFO(this->get_logger(), "Found camera symlink matching pattern '%s': %s (no -video-index0 found, using first match)", 
                    camera_pattern.c_str(), filename.c_str());
    }
    
    // Resolve the symlink to get actual device path
    std::string resolved_path = std::filesystem::read_symlink(symlink_path).string();
    
    if (resolved_path.find("/dev/") == 0) {
        device_path_ = resolved_path;
    } else {
        std::filesystem::path symlink_dir = std::filesystem::path(symlink_path).parent_path();
        std::filesystem::path full_path = std::filesystem::canonical(symlink_dir / resolved_path);
        device_path_ = full_path.string();
    }

    RCLCPP_INFO(this->get_logger(), "=== Maurice Arm Camera Driver (GStreamer) ===");
    RCLCPP_INFO(this->get_logger(), "Camera pattern: %s", camera_pattern.c_str());
    RCLCPP_INFO(this->get_logger(), "Camera symlink: %s", symlink_path.c_str());
    RCLCPP_INFO(this->get_logger(), "Resolved device: %s", device_path_.c_str());
    RCLCPP_INFO(this->get_logger(), "Resolution: %dx%d", width_, height_);
    RCLCPP_INFO(this->get_logger(), "FPS: %.1f", fps_);
    RCLCPP_INFO(this->get_logger(), "Pixel Format: YUYV (via GStreamer)");

    // Create publishers with sensor data QoS profile
    rclcpp::QoS qos = rclcpp::SensorDataQoS()
        .keep_last(10)
        .best_effort()
        .durability_volatile();

    image_pub_ = this->create_publisher<sensor_msgs::msg::Image>("/mars/arm/image_raw", qos);
    
    if (publish_compressed_) {
        compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>("/mars/arm/image_raw/compressed", qos);
    }

    // Initialize camera with retry logic
    const int max_retries = 3;
    const int retry_delay_ms = 1000;
    bool camera_initialized = false;
    
    for (int attempt = 1; attempt <= max_retries; attempt++) {
        RCLCPP_INFO(this->get_logger(), "Initializing camera (attempt %d/%d)...", attempt, max_retries);
        
        if (initializeCamera()) {
            camera_initialized = true;
            break;
        }
        
        if (attempt < max_retries) {
            RCLCPP_WARN(this->get_logger(), "Camera initialization failed, retrying in %d ms...", retry_delay_ms);
            std::this_thread::sleep_for(std::chrono::milliseconds(retry_delay_ms));
        }
    }
    
    if (!camera_initialized) {
        RCLCPP_ERROR(this->get_logger(), "Failed to initialize arm camera after %d attempts", max_retries);
        throw std::runtime_error("Arm camera initialization failed");
    }

    // Start frame processing thread
    frame_thread_running_ = true;
    frame_thread_ = std::thread(&ArmCameraDriver::frameProcessingLoop, this);

    RCLCPP_INFO(this->get_logger(), "Arm camera driver ready");
}

ArmCameraDriver::~ArmCameraDriver() {
    RCLCPP_INFO(this->get_logger(), "Shutting down arm camera driver...");
    
    // Stop frame processing thread
    frame_thread_running_ = false;
    if (frame_thread_.joinable()) {
        frame_thread_.join();
    }
    
    // Release camera with mutex protection
    {
        std::lock_guard<std::mutex> lock(cap_mutex_);
        if (cap_.isOpened()) {
            cap_.release();
        }
    }
    
    RCLCPP_INFO(this->get_logger(), "Arm camera driver shutdown complete");
}

bool ArmCameraDriver::initializeCamera() {
    // Check if device exists
    if (!std::filesystem::exists(device_path_)) {
        RCLCPP_ERROR(this->get_logger(), "Camera device not found: %s", device_path_.c_str());
        return false;
    }

    // Create GStreamer pipeline for YUYV capture
    std::string pipeline = createGStreamerPipeline();
    RCLCPP_INFO(this->get_logger(), "GStreamer pipeline: %s", pipeline.c_str());

    // Open camera with GStreamer backend (mutex protected)
    {
        std::lock_guard<std::mutex> lock(cap_mutex_);
        cap_.open(pipeline, cv::CAP_GSTREAMER);
        
        if (!cap_.isOpened()) {
            RCLCPP_ERROR(this->get_logger(), "Failed to open camera with GStreamer pipeline");
            return false;
        }

        // Verify camera settings
        int actual_width = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_WIDTH));
        int actual_height = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_HEIGHT));
        double actual_fps = cap_.get(cv::CAP_PROP_FPS);
        
        RCLCPP_INFO(this->get_logger(), "Camera opened successfully:");
        RCLCPP_INFO(this->get_logger(), "  Actual resolution: %dx%d", actual_width, actual_height);
        RCLCPP_INFO(this->get_logger(), "  Actual FPS: %.1f", actual_fps);

        if (actual_width != width_ || actual_height != height_) {
            RCLCPP_WARN(this->get_logger(), 
                "Resolution mismatch! Requested: %dx%d, Got: %dx%d",
                width_, height_, actual_width, actual_height);
        }
    }

    return true;
}

std::string ArmCameraDriver::createGStreamerPipeline() {
    // GStreamer pipeline for YUYV capture
    // v4l2src: capture from V4L2 device
    // video/x-raw,format=YUY2: YUYV format (YUY2 is GStreamer's name for YUYV)
    // videoconvert: convert to BGR for OpenCV
    // appsink: output to application
    //   max-buffers=1: only keep newest frame
    //   drop=true: drop old frames if not consumed
    //   sync=false: don't sync to clock (process ASAP)
    
    std::string pipeline = 
        "v4l2src device=" + device_path_ + " io-mode=2 do-timestamp=true ! "
        "video/x-raw,format=YUY2,width=" + std::to_string(width_) + 
        ",height=" + std::to_string(height_) + 
        ",framerate=" + std::to_string(static_cast<int>(fps_)) + "/1 ! "
        "videoconvert ! video/x-raw,format=BGR ! "
        "appsink max-buffers=1 drop=true sync=false";
    
    return pipeline;
}

void ArmCameraDriver::frameProcessingLoop() {
    RCLCPP_INFO(this->get_logger(), "Frame processing loop started");
    
    cv::Mat frame;
    int frame_count = 0;
    
    while (frame_thread_running_ && rclcpp::ok()) {
        try {
            // Capture frame with mutex protection
            bool success = false;
            {
                std::lock_guard<std::mutex> lock(cap_mutex_);
                if (cap_.isOpened()) {
                    success = cap_.read(frame);
                }
            }
            
            if (!success || frame.empty()) {
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                    "Failed to capture frame, attempting recovery...");
                
                // Release camera with mutex protection
                {
                    std::lock_guard<std::mutex> lock(cap_mutex_);
                    cap_.release();
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(1000));
                
                // Check if we should still be running before attempting recovery
                if (!frame_thread_running_ || !rclcpp::ok()) {
                    break;
                }
                
                if (initializeCamera()) {
                    RCLCPP_INFO(this->get_logger(), "Camera reconnected successfully");
                }
                continue;
            }
            
            frame_count++;
            
            // Log every 1000 frames for health monitoring
            if (frame_count % 1000 == 0) {
                RCLCPP_INFO(this->get_logger(), "Camera health check - Frame %d, Device: %s", 
                            frame_count, device_path_.c_str());
            }
            
            // Process and publish frame
            processAndPublishFrame(frame);
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error in frame processing: %s", e.what());
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }
    
    RCLCPP_INFO(this->get_logger(), "Frame processing loop ended");
}

void ArmCameraDriver::processAndPublishFrame(const cv::Mat& frame) {
    auto current_time = this->now();
    
    // Create raw image message
    auto img_msg = std::make_unique<sensor_msgs::msg::Image>();
    img_msg->header.stamp = current_time;
    img_msg->header.frame_id = "arm_camera";
    img_msg->height = frame.rows;
    img_msg->width = frame.cols;
    img_msg->encoding = "bgr8";
    img_msg->is_bigendian = false;
    img_msg->step = frame.cols * 3;
    img_msg->data.assign(frame.data, frame.data + (frame.rows * img_msg->step));

    // Publish with std::move() for zero-copy intra-process communication
    image_pub_->publish(std::move(img_msg));

    // Conditionally publish compressed image at specified interval
    if (publish_compressed_) {
        compressed_frame_counter_++;
        if (compressed_frame_counter_ >= compressed_frame_interval_) {
            compressed_frame_counter_ = 0;
            
            auto compressed_msg = std::make_unique<sensor_msgs::msg::CompressedImage>();
            compressed_msg->header.stamp = current_time;
            compressed_msg->header.frame_id = "arm_camera";
            compressed_msg->format = "jpeg";

            std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 80};
            if (cv::imencode(".jpg", frame, compressed_msg->data, params)) {
                compressed_pub_->publish(std::move(compressed_msg));
            } else {
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                    "Failed to compress image to JPEG");
            }
        }
    }
}

} // namespace maurice_cam

// Register the component
RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::ArmCameraDriver)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    
    try {
        auto node = std::make_shared<maurice_cam::ArmCameraDriver>();
        rclcpp::spin(node);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(rclcpp::get_logger("main"), "Exception: %s", e.what());
        return 1;
    }
    
    rclcpp::shutdown();
    return 0;
}
#endif
