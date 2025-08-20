#include <chrono>
#include <cstdio>
#include <functional>
#include <iostream>
#include <tuple>
#include <string>
#include <vector>
#include <deque>
#include <thread>
#include <atomic>

#include "rclcpp/rclcpp.hpp"
#include "camera_info_manager/camera_info_manager.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"

// DepthAI specific includes
#include "depthai/device/DataQueue.hpp"
#include "depthai/device/Device.hpp"
#include "depthai/pipeline/Pipeline.hpp"
#include "depthai/pipeline/node/ColorCamera.hpp"
#include "depthai/pipeline/node/XLinkOut.hpp"
#include "depthai_bridge/BridgePublisher.hpp"
#include "depthai_bridge/ImageConverter.hpp"

#include <opencv2/opencv.hpp>

using namespace std::chrono_literals;

// Structure to hold resolution dimensions
struct ImageDimensions {
    int width;
    int height;
};

// Creates the DepthAI pipeline for RGB camera output
std::tuple<dai::Pipeline, ImageDimensions> create_rgb_pipeline(
    std::string color_resolution_str,
    float fps) {

    dai::Pipeline pipeline;
    auto colorCam = pipeline.create<dai::node::ColorCamera>();
    auto xlinkOutVideo = pipeline.create<dai::node::XLinkOut>();

    xlinkOutVideo->setStreamName("rgb_video");

    dai::ColorCameraProperties::SensorResolution dai_color_resolution;
    ImageDimensions preview_dimensions = {640, 480};  // Preview/output resolution

    if (color_resolution_str == "800p") {
        dai_color_resolution = dai::ColorCameraProperties::SensorResolution::THE_800_P;
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting color resolution to 800P (1280x800)");
    } else if (color_resolution_str == "720p") {
        dai_color_resolution = dai::ColorCameraProperties::SensorResolution::THE_720_P;
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting color resolution to 720P (1280x720)");
    } else {
        RCLCPP_ERROR(rclcpp::get_logger("rclcpp"), "Invalid color_resolution parameter: %s. Supported: 800p, 720p.", color_resolution_str.c_str());
        throw std::runtime_error("Invalid color camera resolution provided to pipeline creation.");
    }

    colorCam->setBoardSocket(dai::CameraBoardSocket::CAM_A);
    colorCam->setResolution(dai_color_resolution);
    colorCam->setPreviewSize(preview_dimensions.width, preview_dimensions.height);  // Set output resolution
    colorCam->setInterleaved(true);
    colorCam->setColorOrder(dai::ColorCameraProperties::ColorOrder::BGR);
    colorCam->setFps(fps);

    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Camera configured with:");
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Native Resolution: %s", color_resolution_str.c_str());
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Output Resolution: %dx%d", preview_dimensions.width, preview_dimensions.height);
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Interleaved: true");
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Color Order: BGR");
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  FPS: %.2f", fps);

    colorCam->preview.link(xlinkOutVideo->input);  // Use preview output instead of video

    return std::make_tuple(pipeline, preview_dimensions);
}

class CameraDriverNode : public rclcpp::Node {
public:
    CameraDriverNode() : Node("camera_driver"),
                         cinfo_manager_(this),
                         retry_count_(0),
                         max_retries_(5),
                         retry_delay_ms_(1000) {
        // Declare parameters
        this->declare_parameter<std::string>("tf_prefix", "oak");
        this->declare_parameter<std::string>("camera_model", "OAK-D");
        this->declare_parameter<std::string>("color_resolution", "800p");
        this->declare_parameter<double>("fps", 30.0);
        this->declare_parameter<bool>("use_video", true);
        // Device specific parameters
        this->declare_parameter<std::string>("mxId", "");
        this->declare_parameter<bool>("usb2Mode", true);

        // Get parameters
        tf_prefix_ = this->get_parameter("tf_prefix").as_string();
        camera_model_ = this->get_parameter("camera_model").as_string();
        color_resolution_str_ = this->get_parameter("color_resolution").as_string();
        fps_val_ = this->get_parameter("fps").as_double();
        use_video_ = this->get_parameter("use_video").as_bool();
        
        mxId_str_ = this->get_parameter("mxId").as_string();
        usb2Mode_val_ = this->get_parameter("usb2Mode").as_bool();

        RCLCPP_INFO(this->get_logger(), "Initializing Camera Driver Node with parameters:");
        RCLCPP_INFO(this->get_logger(), "  TF Prefix: %s", tf_prefix_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Camera Model: %s", camera_model_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Color Resolution: %s", color_resolution_str_.c_str());
        RCLCPP_INFO(this->get_logger(), "  FPS: %.2f", fps_val_);
        RCLCPP_INFO(this->get_logger(), "  Use Video: %s", use_video_ ? "true" : "false");

        // Now that tf_prefix_ is available, initialize rgb_converter_
        rgb_converter_ = std::make_unique<dai::rosBridge::ImageConverter>(tf_prefix_ + "_rgb_camera_optical_frame", false);

        // Initialize device with retry logic
        initialize_device_with_retry();

        // Initialize frame timing tracking
        frame_timestamps_.clear();
        last_stats_print_ = this->now();
        
        RCLCPP_INFO(this->get_logger(), "Camera driver node core initialized. Publisher setup deferred.");
    }

    void initialize_publishers() {
        if (use_video_) {
            setup_video_publisher();
        }
        RCLCPP_INFO(this->get_logger(), "Camera publishers initialized successfully.");
    }

private:
    void initialize_device_with_retry() {
        for (int attempt = 0; attempt < max_retries_; ++attempt) {
            try {
                initialize_device();
                retry_count_ = 0; // Reset retry count on success
                return;
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), 
                    "Device initialization attempt %d/%d failed: %s", 
                    attempt + 1, max_retries_, e.what());
                
                if (attempt < max_retries_ - 1) {
                    RCLCPP_WARN(this->get_logger(), 
                        "Retrying device initialization in %d ms...", retry_delay_ms_);
                    std::this_thread::sleep_for(std::chrono::milliseconds(retry_delay_ms_));
                    retry_delay_ms_ = std::min(retry_delay_ms_ * 2, 10000); // Exponential backoff, max 10s
                }
            }
        }
        
        RCLCPP_FATAL(this->get_logger(), "Failed to initialize device after %d attempts", max_retries_);
        throw std::runtime_error("Failed to initialize device after maximum retry attempts");
    }

    void initialize_device() {
        ImageDimensions video_dims;
        std::tie(pipeline_, video_dims) = create_rgb_pipeline(color_resolution_str_, fps_val_);

        // Initialize device
        dai::DeviceInfo deviceInfo;
        bool deviceFound = false;
        if (!mxId_str_.empty()) {
            RCLCPP_INFO(this->get_logger(), "Attempting to find device with MXID: %s", mxId_str_.c_str());
            try {
                dai::DeviceInfo di(mxId_str_);
                deviceInfo = di;
                deviceFound = true;
                 RCLCPP_INFO(this->get_logger(), "Device %s found.", mxId_str_.c_str());
            } catch (const std::exception& e) {
                 RCLCPP_WARN(this->get_logger(), "Device with MXID %s not found or error: %s. Will try first available.", mxId_str_.c_str(), e.what());
            }
        }
        
        if (!deviceFound) {
            auto availableDevices = dai::Device::getAllAvailableDevices();
            if(availableDevices.empty()){
                RCLCPP_ERROR(this->get_logger(), "No DepthAI devices found.");
                throw std::runtime_error("No DepthAI devices found.");
            }
            RCLCPP_INFO(this->get_logger(), "Using first available device: %s", availableDevices[0].getMxId().c_str());
            deviceInfo = availableDevices[0];
        }

        device_ = std::make_unique<dai::Device>(pipeline_, deviceInfo, usb2Mode_val_);

        // Get output queues with minimal buffer for lowest latency
        if (use_video_) {
            video_queue_ = device_->getOutputQueue("rgb_video", 1, false);  // Single buffer for minimum latency
        }

        RCLCPP_INFO(this->get_logger(), "Device initialized with queue size: 1 (minimum latency mode)");

        // Calibration and CameraInfo
        calibrationHandler_ = device_->readCalibration();
        
        std::string rgb_camera_name = tf_prefix_;
        cinfo_manager_.setCameraName(rgb_camera_name + "_rgb_camera");
        
        rgb_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
            rgb_converter_->calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_A, video_dims.width, video_dims.height)
        );
    }

    void restart_device() {
        RCLCPP_WARN(this->get_logger(), "Attempting to restart device due to communication error...");
        
        try {
            // Stop the frame processing thread
            if (frame_thread_running_) {
                frame_thread_running_ = false;
                if (frame_thread_.joinable()) {
                    frame_thread_.join();
                }
            }
            
            // Reset device and queues
            video_queue_.reset();
            device_.reset();
            
            // Wait longer for device to recover after crash
            RCLCPP_INFO(this->get_logger(), "Waiting for device to recover...");
            std::this_thread::sleep_for(std::chrono::milliseconds(2000));
            
            // Try to wait for device to become available with timeout
            if (!wait_for_device_availability(10000)) { // 10 second timeout
                throw std::runtime_error("Device did not become available after crash recovery period");
            }
            
            // Reinitialize device
            initialize_device();
            
            // Restart frame processing if we were using video
            if (use_video_) {
                frame_thread_running_ = true;
                frame_thread_ = std::thread(&CameraDriverNode::frame_processing_loop, this);
            }
            
            retry_count_ = 0; // Reset retry count on successful restart
            RCLCPP_INFO(this->get_logger(), "Device successfully restarted");
            
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Failed to restart device: %s", e.what());
            throw;
        }
    }

    bool wait_for_device_availability(int timeout_ms) {
        int elapsed_ms = 0;
        const int check_interval_ms = 500;
        
        while (elapsed_ms < timeout_ms) {
            try {
                auto availableDevices = dai::Device::getAllAvailableDevices();
                
                if (!availableDevices.empty()) {
                    // If we have a specific MXID, check if it's available
                    if (!mxId_str_.empty()) {
                        for (const auto& device : availableDevices) {
                            if (device.getMxId() == mxId_str_) {
                                RCLCPP_INFO(this->get_logger(), "Target device %s is now available", mxId_str_.c_str());
                                return true;
                            }
                        }
                        RCLCPP_INFO(this->get_logger(), "Waiting for specific device %s... (%d/%d ms)", 
                            mxId_str_.c_str(), elapsed_ms, timeout_ms);
                    } else {
                        RCLCPP_INFO(this->get_logger(), "Device is now available");
                        return true;
                    }
                } else {
                    RCLCPP_INFO(this->get_logger(), "No devices available yet... (%d/%d ms)", elapsed_ms, timeout_ms);
                }
            } catch (const std::exception& e) {
                RCLCPP_WARN(this->get_logger(), "Error checking device availability: %s", e.what());
            }
            
            std::this_thread::sleep_for(std::chrono::milliseconds(check_interval_ms));
            elapsed_ms += check_interval_ms;
        }
        
        RCLCPP_ERROR(this->get_logger(), "Timeout waiting for device to become available");
        return false;
    }

    void setup_video_publisher() {
        if (!video_queue_) {
            RCLCPP_ERROR(this->get_logger(), "Video queue is not initialized. Cannot setup video publisher.");
            return;
        }

        // Create publishers with sensor QoS profile
        std::string raw_topic = "/color/image";
        std::string compressed_topic = "/color/image/compressed";
        
        image_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
            raw_topic,
            rclcpp::SensorDataQoS()
        );
        
        compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
            compressed_topic,
            rclcpp::SensorDataQoS()
        );

        RCLCPP_INFO(this->get_logger(), "Created publishers on topics: %s and %s", 
            raw_topic.c_str(), compressed_topic.c_str());

        // Start frame processing thread instead of timer
        frame_thread_running_ = true;
        frame_thread_ = std::thread(&CameraDriverNode::frame_processing_loop, this);
        
        RCLCPP_INFO(this->get_logger(), "Started frame-based processing thread");
    }

    void update_frame_stats() {
        auto current_time = this->now();
        frame_timestamps_.push_back(current_time);
        
        // Remove timestamps older than 1 second
        auto one_second_ago = current_time - rclcpp::Duration::from_nanoseconds(1000000000); // 1 second
        while (!frame_timestamps_.empty() && frame_timestamps_.front() < one_second_ago) {
            frame_timestamps_.pop_front();
        }
        
        // Calculate and print stats every second
        if ((current_time - last_stats_print_).seconds() >= 1.0) {
            print_frame_stats();
            last_stats_print_ = current_time;
        }
    }
    
    void print_frame_stats() {
        if (frame_timestamps_.size() < 2) {
            return; // Need at least 2 frames to calculate intervals
        }
        
        // Calculate average framerate (frames in the last second)
        double avg_framerate = static_cast<double>(frame_timestamps_.size());
        
        // Calculate frame intervals for jitter calculation
        std::vector<double> intervals;
        for (size_t i = 1; i < frame_timestamps_.size(); ++i) {
            double interval = (frame_timestamps_[i] - frame_timestamps_[i-1]).seconds();
            intervals.push_back(interval);
        }
        
        if (intervals.empty()) {
            return;
        }
        
        // Calculate mean interval
        double mean_interval = 0.0;
        for (double interval : intervals) {
            mean_interval += interval;
        }
        mean_interval /= intervals.size();
        
        // Calculate standard deviation (jitter)
        double variance = 0.0;
        for (double interval : intervals) {
            double diff = interval - mean_interval;
            variance += diff * diff;
        }
        variance /= intervals.size();
        double jitter = std::sqrt(variance);
        
        // Convert jitter to milliseconds for readability
        double jitter_ms = jitter * 1000.0;
        double expected_interval = 1.0 / fps_val_;
        double interval_error_ms = (mean_interval - expected_interval) * 1000.0;
        
        RCLCPP_INFO(this->get_logger(), 
            "Frame Stats - FPS: %.1f (target: %.1f) | Jitter: %.2f ms | Timing error: %.2f ms | Samples: %zu",
            avg_framerate, fps_val_, jitter_ms, interval_error_ms, frame_timestamps_.size());
    }

    void frame_processing_loop() {
        RCLCPP_INFO(this->get_logger(), "Frame processing loop started");
        
        // Add timing diagnostics
        auto last_frame_time = std::chrono::high_resolution_clock::now();
        int frame_counter = 0;
        
        while (frame_thread_running_ && rclcpp::ok()) {
            try {
                // Add timing measurement around frame acquisition
                auto acquire_start = std::chrono::high_resolution_clock::now();
                
                // Blocking call - wait for frame
                auto frame = video_queue_->get<dai::ImgFrame>();
                
                auto acquire_end = std::chrono::high_resolution_clock::now();
                auto acquire_duration = std::chrono::duration_cast<std::chrono::microseconds>(acquire_end - acquire_start);
                
                if (frame) {
                    auto imgData = frame->getData();
                    if (!imgData.empty()) {
                        frame_counter++;
                        
                        // Measure actual camera frame timing
                        auto current_frame_time = std::chrono::high_resolution_clock::now();
                        auto camera_interval = std::chrono::duration_cast<std::chrono::microseconds>(current_frame_time - last_frame_time);
                        last_frame_time = current_frame_time;
                        
                        // Update frame timing statistics (for ROS timing)
                        update_frame_stats();
                        
                        auto processing_start = std::chrono::high_resolution_clock::now();
                        
                        // Convert to OpenCV Mat
                        cv::Mat cvFrame(frame->getHeight(), frame->getWidth(), CV_8UC3, imgData.data());
                        
                        // Create and publish raw image message
                        auto current_time = this->now();
                        sensor_msgs::msg::Image rosImage;
                        rosImage.header.stamp = current_time;
                        rosImage.header.frame_id = tf_prefix_ + "_rgb_camera_optical_frame";
                        rosImage.height = frame->getHeight();
                        rosImage.width = frame->getWidth();
                        rosImage.encoding = "bgr8";
                        rosImage.is_bigendian = false;
                        rosImage.step = frame->getWidth() * 3;
                        rosImage.data = std::vector<uint8_t>(imgData.begin(), imgData.end());
                        
                        auto publish_start = std::chrono::high_resolution_clock::now();
                        image_pub_->publish(rosImage);
                        
                        // Create and publish compressed image message
                        sensor_msgs::msg::CompressedImage compressed_msg;
                        compressed_msg.header = rosImage.header;
                        compressed_msg.format = "jpeg";
                        
                        // Use faster compression settings
                        std::vector<int> params = {
                            cv::IMWRITE_JPEG_QUALITY, 60,  // Lower quality for speed
                            cv::IMWRITE_JPEG_OPTIMIZE, 0   // Disable optimization for speed
                        };
                        cv::imencode(".jpg", cvFrame, compressed_msg.data, params);
                        
                        compressed_pub_->publish(compressed_msg);
                        
                        auto processing_end = std::chrono::high_resolution_clock::now();
                        auto processing_duration = std::chrono::duration_cast<std::chrono::microseconds>(processing_end - processing_start);
                        auto publish_duration = std::chrono::duration_cast<std::chrono::microseconds>(processing_end - publish_start);
                        
                        // Log detailed timing every 30 frames
                        if (frame_counter % 30 == 0) {
                            RCLCPP_INFO(this->get_logger(), 
                                "Timing - Camera interval: %ld μs (%.1f fps) | Acquire: %ld μs | Processing: %ld μs | Publish: %ld μs",
                                camera_interval.count(), 
                                1000000.0 / camera_interval.count(),
                                acquire_duration.count(),
                                processing_duration.count(),
                                publish_duration.count());
                        }

                        // Log frame details periodically
                        static int frame_count = 0;
                        if (++frame_count % 150 == 0) {
                            RCLCPP_INFO(this->get_logger(), "Published frame %d - Raw size: %zu bytes, Compressed size: %zu bytes",
                                frame_count, rosImage.data.size(), compressed_msg.data.size());
                        }
                    }
                }
            } catch (const std::runtime_error& e) {
                std::string error_msg = e.what();
                
                // Check if this is a communication error
                if (error_msg.find("Communication exception") != std::string::npos || 
                    error_msg.find("X_LINK_ERROR") != std::string::npos ||
                    error_msg.find("Couldn't read data from stream") != std::string::npos) {
                    
                    RCLCPP_ERROR(this->get_logger(), "Device communication error detected: %s", e.what());
                    
                    if (retry_count_ < max_retries_) {
                        retry_count_++;
                        RCLCPP_WARN(this->get_logger(), "Attempting device restart (attempt %d/%d)", retry_count_, max_retries_);
                        
                        try {
                            restart_device();
                        } catch (const std::exception& restart_e) {
                            RCLCPP_ERROR(this->get_logger(), "Device restart failed: %s", restart_e.what());
                            
                            if (retry_count_ >= max_retries_) {
                                RCLCPP_FATAL(this->get_logger(), "Maximum restart attempts reached. Stopping frame processing.");
                                frame_thread_running_ = false;
                                break;
                            } else {
                                RCLCPP_WARN(this->get_logger(), "Will retry restart in next communication error");
                            }
                        }
                    } else {
                        RCLCPP_FATAL(this->get_logger(), "Maximum restart attempts reached. Stopping frame processing.");
                        frame_thread_running_ = false;
                        break;
                    }
                } else {
                    RCLCPP_ERROR(this->get_logger(), "Non-communication error in frame processing: %s", e.what());
                }
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Unexpected error in frame processing: %s", e.what());
                // Brief pause to prevent rapid error loops
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
        }
        
        RCLCPP_INFO(this->get_logger(), "Frame processing loop ended");
    }

    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
    
    // Replace timer with thread
    std::thread frame_thread_;
    std::atomic<bool> frame_thread_running_{false};

    std::string tf_prefix_;
    std::string camera_model_;
    bool use_video_;

    // Store parameters for device restart
    std::string color_resolution_str_;
    double fps_val_;
    std::string mxId_str_;
    bool usb2Mode_val_;

    dai::Pipeline pipeline_;
    std::unique_ptr<dai::Device> device_;
    std::shared_ptr<dai::DataOutputQueue> video_queue_;
    
    dai::CalibrationHandler calibrationHandler_;
    std::unique_ptr<dai::rosBridge::ImageConverter> rgb_converter_;

    camera_info_manager::CameraInfoManager cinfo_manager_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> rgb_cam_info_;

    // Retry logic variables
    int retry_count_;
    int max_retries_;
    int retry_delay_ms_;

    // Frame timing tracking variables
    std::deque<rclcpp::Time> frame_timestamps_;
    rclcpp::Time last_stats_print_;
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<CameraDriverNode>();
    node->initialize_publishers(); // Call to initialize publishers after node is fully constructed
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}