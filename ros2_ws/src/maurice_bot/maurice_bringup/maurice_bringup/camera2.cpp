#include <chrono>
#include <functional>
#include <iostream>
#include <string>
#include <vector>
#include <deque>
#include <thread>
#include <atomic>
#include <filesystem>
#include <regex>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"

#include <opencv2/opencv.hpp>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>

using namespace std::chrono_literals;

class USB3DCameraNode : public rclcpp::Node {
public:
    USB3DCameraNode() : Node("usb_3d_camera_driver") {
        // Declare parameters
        this->declare_parameter<std::string>("camera_symlink", "usb-3D_USB_Camera_3D_USB_Camera_01.00.00-video-index0");
        this->declare_parameter<int>("width", 1280);
        this->declare_parameter<int>("height", 480);
        this->declare_parameter<double>("fps", 30.0);
        this->declare_parameter<std::string>("tf_prefix", "usb_3d");
        this->declare_parameter<int>("jpeg_quality", 60);

        // Get parameters
        camera_symlink_ = this->get_parameter("camera_symlink").as_string();
        capture_width_ = this->get_parameter("width").as_int();
        capture_height_ = this->get_parameter("height").as_int();
        fps_val_ = this->get_parameter("fps").as_double();
        tf_prefix_ = this->get_parameter("tf_prefix").as_string();
        jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();

        // Resolve symlink to device path
        std::string symlink_path = "/dev/v4l/by-id/" + camera_symlink_;
        if (!std::filesystem::exists(symlink_path)) {
            RCLCPP_ERROR(this->get_logger(), "Camera symlink not found: %s", symlink_path.c_str());
            throw std::runtime_error("Camera symlink not found");
        }
        
        // Resolve the symlink to get actual device path
        std::string resolved_path = std::filesystem::read_symlink(symlink_path).string();
        
        // Handle relative paths properly
        if (resolved_path.find("/dev/") == 0) {
            // Already absolute path
            device_path_ = resolved_path;
        } else {
            // Relative path, resolve it properly
            std::filesystem::path symlink_dir = std::filesystem::path(symlink_path).parent_path();
            std::filesystem::path full_path = std::filesystem::canonical(symlink_dir / resolved_path);
            device_path_ = full_path.string();
        }

        // Calculate left image dimensions (half width for stereo camera)
        left_width_ = capture_width_ / 2;
        left_height_ = capture_height_;

        RCLCPP_INFO(this->get_logger(), "Initializing USB 3D Camera Driver Node with parameters:");
        RCLCPP_INFO(this->get_logger(), "  Camera Symlink: %s", camera_symlink_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Device Path: %s", device_path_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Capture Resolution: %dx%d", capture_width_, capture_height_);
        RCLCPP_INFO(this->get_logger(), "  Left Image Resolution: %dx%d", left_width_, left_height_);
        RCLCPP_INFO(this->get_logger(), "  FPS: %.2f", fps_val_);
        RCLCPP_INFO(this->get_logger(), "  TF Prefix: %s", tf_prefix_.c_str());
        RCLCPP_INFO(this->get_logger(), "  JPEG Quality: %d", jpeg_quality_);

        // Initialize camera
        initialize_camera();

        // Initialize frame timing tracking
        frame_timestamps_.clear();
        last_stats_print_ = this->now();
        
        RCLCPP_INFO(this->get_logger(), "USB 3D Camera driver node initialized.");
    }

    ~USB3DCameraNode() {
        // Stop the frame processing thread
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
    }

    void initialize_publishers() {
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

        // Start frame processing thread
        frame_thread_running_ = true;
        frame_thread_ = std::thread(&USB3DCameraNode::frame_processing_loop, this);
        
        RCLCPP_INFO(this->get_logger(), "Started frame processing thread");
    }

private:
    int camera_fd_ = -1;
    
    void set_camera_controls_to_defaults() {
        // Open the device file directly
        camera_fd_ = open(device_path_.c_str(), O_RDWR);
        if (camera_fd_ < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to open camera device for controls: %s", 
                        strerror(errno));
            return;
        }
        
        RCLCPP_INFO(this->get_logger(), "Setting camera controls to default values");
        
        // User Controls
        set_v4l2_control_programmatic(V4L2_CID_BRIGHTNESS, 0);
        set_v4l2_control_programmatic(V4L2_CID_CONTRAST, 0);
        set_v4l2_control_programmatic(V4L2_CID_SATURATION, 64);
        set_v4l2_control_programmatic(V4L2_CID_HUE, 0);
        set_v4l2_control_programmatic(V4L2_CID_AUTO_WHITE_BALANCE, 1);
        set_v4l2_control_programmatic(V4L2_CID_GAMMA, 100);
        set_v4l2_control_programmatic(V4L2_CID_GAIN, 110);
        set_v4l2_control_programmatic(V4L2_CID_POWER_LINE_FREQUENCY, 1);
        set_v4l2_control_programmatic(V4L2_CID_WHITE_BALANCE_TEMPERATURE, 4600);
        set_v4l2_control_programmatic(V4L2_CID_SHARPNESS, 0);
        set_v4l2_control_programmatic(V4L2_CID_BACKLIGHT_COMPENSATION, 80);
        
        // Camera Controls
        set_v4l2_control_programmatic(V4L2_CID_EXPOSURE_AUTO, 3); // Aperture Priority
        set_v4l2_control_programmatic(V4L2_CID_EXPOSURE_ABSOLUTE, 156);
        
        close(camera_fd_);
        camera_fd_ = -1;
        RCLCPP_INFO(this->get_logger(), "Camera controls set to default values");
    }
    
    void set_v4l2_control_programmatic(int control_id, int value) {
        struct v4l2_control ctrl;
        ctrl.id = control_id;
        ctrl.value = value;
        
        if (ioctl(camera_fd_, VIDIOC_S_CTRL, &ctrl) == 0) {
            RCLCPP_DEBUG(this->get_logger(), "Set control 0x%x = %d", control_id, value);
        } else {
            RCLCPP_WARN(this->get_logger(), "Failed to set control 0x%x = %d: %s", 
                       control_id, value, strerror(errno));
        }
    }
    
    void initialize_camera() {
        RCLCPP_INFO(this->get_logger(), "Opening camera device: %s", device_path_.c_str());
        
        // Create GStreamer pipeline string - simplified for OpenCV compatibility
        std::string gst_pipeline = 
            "v4l2src device=" + device_path_ + " ! "
            "image/jpeg,width=" + std::to_string(capture_width_) + 
            ",height=" + std::to_string(capture_height_) + 
            ",framerate=" + std::to_string(static_cast<int>(fps_val_)) + "/1 ! "
            "jpegdec ! videoconvert ! appsink";
        
        RCLCPP_INFO(this->get_logger(), "GStreamer pipeline: %s", gst_pipeline.c_str());
        
        // Open camera with GStreamer backend
        cap_.open(gst_pipeline, cv::CAP_GSTREAMER);
        
        if (!cap_.isOpened()) {
            RCLCPP_ERROR(this->get_logger(), "Failed to open camera with GStreamer pipeline");
            RCLCPP_ERROR(this->get_logger(), "Pipeline: %s", gst_pipeline.c_str());
            throw std::runtime_error("Failed to open camera with GStreamer pipeline");
        }

        // Verify settings
        int actual_width = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_WIDTH));
        int actual_height = static_cast<int>(cap_.get(cv::CAP_PROP_FRAME_HEIGHT));
        double actual_fps = cap_.get(cv::CAP_PROP_FPS);
        
        RCLCPP_INFO(this->get_logger(), "Camera opened successfully with GStreamer:");
        RCLCPP_INFO(this->get_logger(), "  Actual Resolution: %dx%d", actual_width, actual_height);
        RCLCPP_INFO(this->get_logger(), "  Actual FPS: %.2f", actual_fps);
        RCLCPP_INFO(this->get_logger(), "  Backend: GStreamer with hardware acceleration");

        if (actual_width != capture_width_ || actual_height != capture_height_) {
            RCLCPP_WARN(this->get_logger(), 
                "Camera resolution mismatch! Requested: %dx%d, Got: %dx%d",
                capture_width_, capture_height_, actual_width, actual_height);
        }
    }

    void update_frame_stats() {
        auto current_time = this->now();
        frame_timestamps_.push_back(current_time);
        
        // Remove timestamps older than 1 second
        auto one_second_ago = current_time - rclcpp::Duration::from_nanoseconds(1000000000);
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
            return;
        }
        
        // Calculate average framerate
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
        
        // Convert to milliseconds
        double jitter_ms = jitter * 1000.0;
        double expected_interval = 1.0 / fps_val_;
        double interval_error_ms = (mean_interval - expected_interval) * 1000.0;
        
        RCLCPP_INFO(this->get_logger(), 
            "Frame Stats - FPS: %.1f (target: %.1f) | Jitter: %.2f ms | Timing error: %.2f ms | Samples: %zu",
            avg_framerate, fps_val_, jitter_ms, interval_error_ms, frame_timestamps_.size());
    }

    void frame_processing_loop() {
        RCLCPP_INFO(this->get_logger(), "Frame processing loop started");
        
        auto last_frame_time = std::chrono::high_resolution_clock::now();
        int frame_counter = 0;
        
        while (frame_thread_running_ && rclcpp::ok()) {
            try {
                auto acquire_start = std::chrono::high_resolution_clock::now();
                
                cv::Mat frame;
                bool success = cap_.read(frame);
                
                auto acquire_end = std::chrono::high_resolution_clock::now();
                auto acquire_duration = std::chrono::duration_cast<std::chrono::microseconds>(acquire_end - acquire_start);
                
                if (!success || frame.empty()) {
                    RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000, 
                        "Failed to capture frame from camera");
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                    continue;
                }
                
                frame_counter++;
                
                // Measure camera frame timing
                auto current_frame_time = std::chrono::high_resolution_clock::now();
                auto camera_interval = std::chrono::duration_cast<std::chrono::microseconds>(current_frame_time - last_frame_time);
                last_frame_time = current_frame_time;
                
                // Update frame timing statistics
                update_frame_stats();
                
                auto processing_start = std::chrono::high_resolution_clock::now();
                
                // Extract left half of the stereo image (left camera only)
                cv::Mat left_frame = frame(cv::Rect(0, 0, left_width_, left_height_)).clone();
                
                // Rotate the image 180 degrees
                cv::rotate(left_frame, left_frame, cv::ROTATE_180);
                
                // Create and publish raw image message
                auto current_time = this->now();
                sensor_msgs::msg::Image rosImage;
                rosImage.header.stamp = current_time;
                rosImage.header.frame_id = tf_prefix_ + "_rgb_camera_optical_frame";
                rosImage.height = left_frame.rows;
                rosImage.width = left_frame.cols;
                rosImage.encoding = "bgr8";
                rosImage.is_bigendian = false;
                rosImage.step = left_frame.cols * 3;
                
                // Copy image data
                size_t data_size = left_frame.total() * left_frame.elemSize();
                rosImage.data.resize(data_size);
                std::memcpy(rosImage.data.data(), left_frame.data, data_size);
                
                auto publish_start = std::chrono::high_resolution_clock::now();
                image_pub_->publish(rosImage);
                
                // Create and publish compressed image message
                sensor_msgs::msg::CompressedImage compressed_msg;
                compressed_msg.header = rosImage.header;
                compressed_msg.format = "jpeg";
                
                // Compress with specified quality
                std::vector<int> params = {
                    cv::IMWRITE_JPEG_QUALITY, jpeg_quality_,
                    cv::IMWRITE_JPEG_OPTIMIZE, 0
                };
                cv::imencode(".jpg", left_frame, compressed_msg.data, params);
                
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
                if (frame_counter % 150 == 0) {
                    RCLCPP_INFO(this->get_logger(), 
                        "Published frame %d - Raw size: %zu bytes, Compressed size: %zu bytes",
                        frame_counter, rosImage.data.size(), compressed_msg.data.size());
                }
                
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Error in frame processing: %s", e.what());
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
        }
        
        RCLCPP_INFO(this->get_logger(), "Frame processing loop ended");
    }

    // ROS publishers
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
    
    // Thread for frame processing
    std::thread frame_thread_;
    std::atomic<bool> frame_thread_running_{false};

    // Camera parameters
    std::string camera_symlink_;
    std::string device_path_;
    int capture_width_;
    int capture_height_;
    int left_width_;
    int left_height_;
    double fps_val_;
    std::string tf_prefix_;
    int jpeg_quality_;

    // OpenCV camera capture
    cv::VideoCapture cap_;

    // Frame timing tracking
    std::deque<rclcpp::Time> frame_timestamps_;
    rclcpp::Time last_stats_print_;
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<USB3DCameraNode>();
    node->initialize_publishers();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}

