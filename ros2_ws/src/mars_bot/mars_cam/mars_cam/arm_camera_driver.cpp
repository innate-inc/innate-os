// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#include "mars_cam/arm_camera_driver.hpp"
#include <filesystem>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <cerrno>
#include <cstring>

#include "mars_cam/camera_by_id.hpp"

using namespace std::chrono_literals;

namespace mars_cam {

ArmCameraDriver::ArmCameraDriver(const rclcpp::NodeOptions& options) : Node("arm_camera_driver", options) {
    RCLCPP_DEBUG(this->get_logger(), "Initializing arm camera driver...");

    // Parameters
    this->declare_parameter<std::string>("camera_symlink", "Arducam");
    this->declare_parameter<int>("width", 640);
    this->declare_parameter<int>("height", 480);
    this->declare_parameter<double>("fps", 30.0);
    this->declare_parameter<std::string>("pixel_format", "YUYV");
    this->declare_parameter<bool>("publish_compressed", false);
    this->declare_parameter<int>("compressed_frame_interval", 5);
    this->declare_parameter<int>("power_line_frequency", 60);  // Anti-flicker filter: 0=disabled, 50 or 60 (Hz)

    // Get parameters
    std::string camera_symlink = this->get_parameter("camera_symlink").as_string();
    width_ = this->get_parameter("width").as_int();
    height_ = this->get_parameter("height").as_int();
    fps_ = this->get_parameter("fps").as_double();
    publish_compressed_ = this->get_parameter("publish_compressed").as_bool();
    compressed_frame_interval_ = this->get_parameter("compressed_frame_interval").as_int();
    power_line_frequency_ = this->get_parameter("power_line_frequency").as_int();

    device_path_ = findCameraByIdPath(camera_symlink);
    if (device_path_.empty()) {
        RCLCPP_ERROR(this->get_logger(), "No camera matching '%s' in /dev/v4l/by-id", camera_symlink.c_str());
        throw std::runtime_error("Camera symlink not found");
    }

    RCLCPP_DEBUG(this->get_logger(), "=== Mars Arm Camera Driver (GStreamer) ===");
    RCLCPP_DEBUG(this->get_logger(), "Camera pattern: %s", camera_symlink.c_str());
    RCLCPP_INFO(this->get_logger(), "Camera device: %s (%s) @ %dx%d, %.1f FPS", device_path_.c_str(),
                currentVideoNode(device_path_).c_str(), width_, height_, fps_);
    RCLCPP_DEBUG(this->get_logger(), "Resolution: %dx%d", width_, height_);
    RCLCPP_DEBUG(this->get_logger(), "FPS: %.1f", fps_);
    RCLCPP_DEBUG(this->get_logger(), "Pixel Format: YUYV (via GStreamer)");

    // Create publishers with sensor data QoS profile
    rclcpp::QoS qos = rclcpp::SensorDataQoS().keep_last(2).best_effort().durability_volatile();

    image_pub_ = this->create_publisher<sensor_msgs::msg::Image>("/mars/arm/image_raw", qos);

    if (publish_compressed_) {
        compressed_pub_ =
            this->create_publisher<sensor_msgs::msg::CompressedImage>("/mars/arm/image_raw/compressed", qos);
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
    RCLCPP_DEBUG(this->get_logger(), "GStreamer pipeline: %s", pipeline.c_str());

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
            RCLCPP_WARN(this->get_logger(), "Resolution mismatch! Requested: %dx%d, Got: %dx%d", width_, height_,
                        actual_width, actual_height);
        }
    }

    applyPowerLineFrequency();

    return true;
}

void ArmCameraDriver::applyPowerLineFrequency() {
    // Anti-flicker filter: match the camera's banding filter to the local mains
    // frequency so indoor lighting doesn't flicker in auto-exposure mode.
    int v4l2_value;
    switch (power_line_frequency_) {
        case 0:
            v4l2_value = V4L2_CID_POWER_LINE_FREQUENCY_DISABLED;
            break;
        case 50:
            v4l2_value = V4L2_CID_POWER_LINE_FREQUENCY_50HZ;
            break;
        case 60:
            v4l2_value = V4L2_CID_POWER_LINE_FREQUENCY_60HZ;
            break;
        default:
            RCLCPP_WARN(this->get_logger(),
                        "Invalid power_line_frequency %d (expected 0, 50 or 60), keeping camera default",
                        power_line_frequency_);
            return;
    }

    int fd = open(device_path_.c_str(), O_RDWR);
    if (fd == -1) {
        RCLCPP_WARN(this->get_logger(), "Failed to open %s for V4L2 controls: %s", device_path_.c_str(),
                    strerror(errno));
        return;
    }

    struct v4l2_control ctrl;
    ctrl.id = V4L2_CID_POWER_LINE_FREQUENCY;
    ctrl.value = v4l2_value;
    if (ioctl(fd, VIDIOC_S_CTRL, &ctrl) == -1) {
        RCLCPP_WARN(this->get_logger(), "Failed to set anti-flicker filter to %d Hz: %s", power_line_frequency_,
                    strerror(errno));
    } else {
        RCLCPP_INFO(this->get_logger(), "Anti-flicker (power line) filter set to %d Hz", power_line_frequency_);
    }
    close(fd);
}

std::string ArmCameraDriver::createGStreamerPipeline() {
    // nvvidconv does the YUY2->BGRx colorspace conversion on the Jetson VIC
    // engine instead of the CPU; the trailing videoconvert is just the cheap
    // BGRx->BGR stride drop for OpenCV.
    std::string pipeline = "v4l2src device=" + device_path_ +
                           " io-mode=2 do-timestamp=true ! "
                           "video/x-raw,format=YUY2,width=" +
                           std::to_string(width_) + ",height=" + std::to_string(height_) +
                           ",framerate=" + std::to_string(static_cast<int>(fps_)) +
                           "/1 ! "
                           "nvvidconv ! video/x-raw,format=BGRx ! "
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
                RCLCPP_DEBUG(this->get_logger(), "Camera health check - Frame %d, Device: %s", frame_count,
                             device_path_.c_str());
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
            if (compressed_pub_->get_subscription_count() == 0) {
                return;
            }

            auto compressed_msg = std::make_unique<sensor_msgs::msg::CompressedImage>();
            compressed_msg->header.stamp = current_time;
            compressed_msg->header.frame_id = "arm_camera";
            compressed_msg->format = "jpeg";

            std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 80};
            if (cv::imencode(".jpg", frame, compressed_msg->data, params)) {
                compressed_pub_->publish(std::move(compressed_msg));
            } else {
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "Failed to compress image to JPEG");
            }
        }
    }
}

}  // namespace mars_cam

// Register the component
RCLCPP_COMPONENTS_REGISTER_NODE(mars_cam::ArmCameraDriver)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char** argv) {
    rclcpp::init(argc, argv);

    try {
        auto node = std::make_shared<mars_cam::ArmCameraDriver>();
        rclcpp::spin(node);
    } catch (const std::exception& e) {
        RCLCPP_ERROR(rclcpp::get_logger("arm_camera_driver"), "Exception: %s", e.what());
        return 1;
    }

    rclcpp::shutdown();
    return 0;
}
#endif
