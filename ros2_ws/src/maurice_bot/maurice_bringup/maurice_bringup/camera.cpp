#include <chrono>
#include <cstdio>
#include <functional>
#include <iostream>
#include <tuple>
#include <string>
#include <vector>
#include <deque>
#include <filesystem>
#include <iomanip>
#include <sstream>

#include "rclcpp/rclcpp.hpp"
#include "camera_info_manager/camera_info_manager.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/camera_info.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "stereo_msgs/msg/disparity_image.hpp"

// DepthAI specific includes
#include "depthai/device/DataQueue.hpp"
#include "depthai/device/Device.hpp"
#include "depthai/pipeline/Pipeline.hpp"
#include "depthai/pipeline/node/ColorCamera.hpp"
#include "depthai/pipeline/node/MonoCamera.hpp"
#include "depthai/pipeline/node/XLinkOut.hpp"
#include "depthai/pipeline/node/StereoDepth.hpp"
#include "depthai_bridge/BridgePublisher.hpp"
#include "depthai_bridge/ImageConverter.hpp"

#include <opencv2/opencv.hpp>
#include <opencv2/calib3d.hpp>

using namespace std::chrono_literals;

// Structure to hold resolution dimensions
struct ImageDimensions {
    int width;
    int height;
};

// Combined pipeline creation for both RGB and stereo cameras
std::tuple<dai::Pipeline, ImageDimensions, ImageDimensions> create_combined_pipeline(
    std::string color_resolution_str,
    std::string stereo_resolution_str,
    float fps,
    bool enable_stereo) {

    dai::Pipeline pipeline;
    
    // Create color camera
    auto colorCam = pipeline.create<dai::node::ColorCamera>();
    auto xlinkOutRgb = pipeline.create<dai::node::XLinkOut>();
    xlinkOutRgb->setStreamName("rgb_video");
    
    // Create mono cameras for stereo (only if enabled)
    std::shared_ptr<dai::node::MonoCamera> monoLeft, monoRight;
    std::shared_ptr<dai::node::StereoDepth> stereo;
    std::shared_ptr<dai::node::XLinkOut> xoutLeft, xoutRight;
    
    if (enable_stereo) {
        monoLeft = pipeline.create<dai::node::MonoCamera>();
        monoRight = pipeline.create<dai::node::MonoCamera>();
        stereo = pipeline.create<dai::node::StereoDepth>();

        // Create XLinkOut nodes for stereo
        xoutLeft = pipeline.create<dai::node::XLinkOut>();
        xoutRight = pipeline.create<dai::node::XLinkOut>();

        std::string left_stream_name = "left_video";
        std::string right_stream_name = "right_video";
        xoutLeft->setStreamName(left_stream_name);
        xoutRight->setStreamName(right_stream_name);
    }

    // Configure color camera
    dai::ColorCameraProperties::SensorResolution dai_color_resolution;
    ImageDimensions color_preview_dimensions = {640, 480};  // Preview/output resolution

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
    colorCam->setPreviewSize(color_preview_dimensions.width, color_preview_dimensions.height);
    colorCam->setInterleaved(true);
    colorCam->setColorOrder(dai::ColorCameraProperties::ColorOrder::BGR);
    colorCam->setFps(fps);

    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Color camera configured with:");
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Native Resolution: %s", color_resolution_str.c_str());
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Output Resolution: %dx%d", color_preview_dimensions.width, color_preview_dimensions.height);
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Color Order: BGR");
    RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  FPS: %.2f", fps);

    colorCam->preview.link(xlinkOutRgb->input);

    // Configure stereo cameras (only if enabled)
    ImageDimensions stereo_preview_dimensions = {640, 400}; // Default dimensions 
    
    if (enable_stereo) {
        dai::MonoCameraProperties::SensorResolution dai_stereo_resolution;

        if (stereo_resolution_str == "800p") {
            dai_stereo_resolution = dai::MonoCameraProperties::SensorResolution::THE_800_P;
            stereo_preview_dimensions = {1280, 800};
            RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting mono resolution to 800P (1280x800)");
        } else if (stereo_resolution_str == "720p") {
            dai_stereo_resolution = dai::MonoCameraProperties::SensorResolution::THE_720_P;
            stereo_preview_dimensions = {1280, 720};
            RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting mono resolution to 720P (1280x720)");
        } else if (stereo_resolution_str == "400p") {
            dai_stereo_resolution = dai::MonoCameraProperties::SensorResolution::THE_400_P;
            stereo_preview_dimensions = {640, 400};
            RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting mono resolution to 400P (640x400)");
        }
        else {
            RCLCPP_ERROR(rclcpp::get_logger("rclcpp"), "Invalid stereo resolution parameter: %s. Supported: 800p, 720p, 400p.", stereo_resolution_str.c_str());
            throw std::runtime_error("Invalid mono camera resolution provided to pipeline creation.");
        }
        
        // Configure left camera
        monoLeft->setBoardSocket(dai::CameraBoardSocket::CAM_B);
        monoLeft->setResolution(dai_stereo_resolution);
        monoLeft->setFps(fps);

        // Configure right camera
        monoRight->setBoardSocket(dai::CameraBoardSocket::CAM_C);
        monoRight->setResolution(dai_stereo_resolution);
        monoRight->setFps(fps);

        // StereoDepth configuration for rectification
        stereo->setRectifyEdgeFillColor(0); // Black, to better see the cutout
        stereo->setLeftRightCheck(true);
        stereo->setExtendedDisparity(false);
        stereo->setSubpixel(false);

        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Stereo cameras configured with:");
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  Resolution: %s", stereo_resolution_str.c_str());
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "  FPS: %.2f", fps);

        // Link cameras to stereo node
        monoLeft->out.link(stereo->left);
        monoRight->out.link(stereo->right);

        // Link rectified outputs to XLinkOut
        stereo->rectifiedLeft.link(xoutLeft->input);
        stereo->rectifiedRight.link(xoutRight->input);
    } else {
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Stereo cameras disabled - skipping stereo pipeline creation");
    }

    return std::make_tuple(pipeline, color_preview_dimensions, stereo_preview_dimensions);
}

class CameraDriverNode : public rclcpp::Node {
public:
    CameraDriverNode() : Node("camera_driver"),
                         cinfo_manager_left_(this, "left_camera"),
                         cinfo_manager_right_(this, "right_camera"),
                         cinfo_manager_rgb_(this),
                         retry_count_(0),
                         max_retries_(5),
                         retry_delay_ms_(1000) {
        // Declare parameters
        this->declare_parameter<std::string>("tf_prefix", "oak");
        this->declare_parameter<std::string>("camera_model", "OAK-D");
        this->declare_parameter<std::string>("color_resolution", "800p");
        this->declare_parameter<std::string>("resolution", "400p");
        this->declare_parameter<double>("fps", 30.0);
        this->declare_parameter<bool>("use_video", true);
        this->declare_parameter<int>("stereo_confidence_threshold", 245);
        // Disparity calculation parameters
        this->declare_parameter<int>("sgbm_num_disparities", 64);
        this->declare_parameter<int>("sgbm_block_size", 3);
        // Device specific parameters
        this->declare_parameter<std::string>("mxId", "");
        this->declare_parameter<bool>("usb2Mode", false);
        // Debug parameters
        this->declare_parameter<bool>("debug_save_images", true);
        this->declare_parameter<std::string>("debug_output_dir", "~/Pictures/camera_debug");
        this->declare_parameter<int>("debug_save_interval", 30); // Save every N frames

        // Disparity parameters
        this->declare_parameter<bool>("enable_disparity", true);

        // Performance parameters
        this->declare_parameter<int>("jpeg_quality", 80);
        this->declare_parameter<bool>("publish_stereo_compressed", true);
        this->declare_parameter<double>("rgb_fps", 30.0);
        this->declare_parameter<double>("stereo_fps", 15.0);
        this->declare_parameter<bool>("enable_stereo", false);

        // Get parameters
        tf_prefix_ = this->get_parameter("tf_prefix").as_string();
        camera_model_ = this->get_parameter("camera_model").as_string();
        color_resolution_str_ = this->get_parameter("color_resolution").as_string();
        resolution_str_ = this->get_parameter("resolution").as_string();
        fps_val_ = this->get_parameter("fps").as_double();
        use_video_ = this->get_parameter("use_video").as_bool();
        stereo_confidence_threshold_ = this->get_parameter("stereo_confidence_threshold").as_int();
        
        mxId_str_ = this->get_parameter("mxId").as_string();
        usb2Mode_val_ = this->get_parameter("usb2Mode").as_bool();

        // Disparity parameters
        int sgbm_num_disparities = this->get_parameter("sgbm_num_disparities").as_int();
        int sgbm_block_size = this->get_parameter("sgbm_block_size").as_int();

        // Debug parameters
        debug_save_images_ = this->get_parameter("debug_save_images").as_bool();
        debug_output_dir_ = this->get_parameter("debug_output_dir").as_string();
        debug_save_interval_ = this->get_parameter("debug_save_interval").as_int();

        // Disparity parameters
        enable_disparity_ = this->get_parameter("enable_disparity").as_bool();
        RCLCPP_INFO(this->get_logger(), "DEBUG: enable_disparity parameter read as: %s", enable_disparity_ ? "true" : "false");

        // Performance parameters
        jpeg_quality_ = this->get_parameter("jpeg_quality").as_int();
        publish_stereo_compressed_ = this->get_parameter("publish_stereo_compressed").as_bool();
        rgb_fps_ = this->get_parameter("rgb_fps").as_double();
        stereo_fps_ = this->get_parameter("stereo_fps").as_double();
        enable_stereo_ = this->get_parameter("enable_stereo").as_bool();

        RCLCPP_INFO(this->get_logger(), "Initializing Camera Driver Node with parameters:");
        RCLCPP_INFO(this->get_logger(), "  Color Resolution: %s", color_resolution_str_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Stereo Resolution: %s", resolution_str_.c_str());
        RCLCPP_INFO(this->get_logger(), "  FPS: %.2f", fps_val_);
        RCLCPP_INFO(this->get_logger(), "  Use Video: %s", use_video_ ? "true" : "false");
        RCLCPP_INFO(this->get_logger(), "  Enable Disparity: %s", enable_disparity_ ? "true" : "false");
        RCLCPP_INFO(this->get_logger(), "  JPEG Quality: %d", jpeg_quality_);
        RCLCPP_INFO(this->get_logger(), "  Publish Stereo Compressed: %s", publish_stereo_compressed_ ? "true" : "false");
        RCLCPP_INFO(this->get_logger(), "  RGB FPS: %.2f", rgb_fps_);
        RCLCPP_INFO(this->get_logger(), "  Stereo FPS: %.2f", stereo_fps_);
        RCLCPP_INFO(this->get_logger(), "  Enable Stereo: %s", enable_stereo_ ? "true" : "false");

        // Initialize RGB converter
        rgb_converter_ = std::make_unique<dai::rosBridge::ImageConverter>(tf_prefix_ + "_rgb_camera_optical_frame", false);

        // Dynamically calculate P1 and P2 based on block size
        int p1 = 8 * 1 * sgbm_block_size * sgbm_block_size;
        int p2 = 32 * 1 * sgbm_block_size * sgbm_block_size;

        // Initialize StereoSGBM
        if (enable_disparity_) {
            sgbm_ = cv::StereoSGBM::create(
                0,    // minDisparity
                sgbm_num_disparities,   // numDisparities
                sgbm_block_size,    // blockSize
                p1,  // P1
                p2,  // P2
                1,    // disp12MaxDiff
                0,    // preFilterCap
                10,   // uniquenessRatio
                100,  // speckleWindowSize
                32,   // speckleRange
                cv::StereoSGBM::MODE_SGBM
            );
            RCLCPP_INFO(this->get_logger(), "StereoSGBM algorithm initialized.");
        } else {
            RCLCPP_INFO(this->get_logger(), "Disparity computation disabled - skipping StereoSGBM initialization.");
        }

        // Initialize debug directory if needed
        if (debug_save_images_) {
            initialize_debug_directory();
        }

        // Initialize device with retry logic
        initialize_device_with_retry();

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
        ImageDimensions color_dims, stereo_dims;
        
        std::tie(pipeline_, color_dims, stereo_dims) = create_combined_pipeline(color_resolution_str_, resolution_str_, fps_val_, enable_stereo_);

        // Initialize device
        dai::DeviceInfo deviceInfo; // Default constructor for first available device
        bool deviceFound = false;
        if (!mxId_str_.empty()) {
            RCLCPP_INFO(this->get_logger(), "Attempting to find device with MXID: %s", mxId_str_.c_str());
            try {
                dai::DeviceInfo di(mxId_str_); // Try to find by MXID
                deviceInfo = di;
                deviceFound = true;
                 RCLCPP_INFO(this->get_logger(), "Device %s found.", mxId_str_.c_str());
            } catch (const std::exception& e) {
                 RCLCPP_WARN(this->get_logger(), "Device with MXID %s not found or error: %s. Will try first available.", mxId_str_.c_str(), e.what());
            }
        }
        
        if (!deviceFound) { // If not found by MXID or MXID not specified, get first available
            auto availableDevices = dai::Device::getAllAvailableDevices();
            if(availableDevices.empty()){
                RCLCPP_ERROR(this->get_logger(), "No DepthAI devices found.");
                throw std::runtime_error("No DepthAI devices found.");
            }
            RCLCPP_INFO(this->get_logger(), "Using first available device: %s", availableDevices[0].getMxId().c_str());
            deviceInfo = availableDevices[0];
        }

        device_ = std::make_unique<dai::Device>(pipeline_, deviceInfo, usb2Mode_val_);

        // Get output queues
        if (use_video_) {
            rgb_video_queue_ = device_->getOutputQueue("rgb_video", 8, false);
            
            if (enable_stereo_) {
                left_video_queue_ = device_->getOutputQueue("left_video", 8, false);
                right_video_queue_ = device_->getOutputQueue("right_video", 8, false);
            }
        }

        // Calibration and CameraInfo
        calibrationHandler_ = device_->readCalibration();
        
        // RGB camera
        std::string rgb_camera_name = tf_prefix_;
        cinfo_manager_rgb_.setCameraName(rgb_camera_name + "_rgb_camera");
        rgb_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
            rgb_converter_->calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_A, color_dims.width, color_dims.height)
        );
        
        // Stereo camera calibration (only if enabled)
        if (enable_stereo_) {
            // Left camera
            std::string left_camera_name = tf_prefix_ + "_left";
            cinfo_manager_left_.setCameraName(left_camera_name + "_camera");
            dai::rosBridge::ImageConverter left_converter(tf_prefix_ + "_left_camera_frame", false);
            left_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
                left_converter.calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_B, stereo_dims.width, stereo_dims.height)
            );

            // Right camera
            std::string right_camera_name = tf_prefix_ + "_right";
            cinfo_manager_right_.setCameraName(right_camera_name + "_camera");
            dai::rosBridge::ImageConverter right_converter(tf_prefix_ + "_right_camera_frame", false);
            right_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
                right_converter.calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_C, stereo_dims.width, stereo_dims.height)
            );
        }
    }

    void restart_device() {
        RCLCPP_WARN(this->get_logger(), "Attempting to restart device due to communication error...");
        
        try {
            // Stop the publishing timer
            if (publish_timer_) {
                publish_timer_->cancel();
                publish_timer_.reset();
            }
            
            // Reset device and queues
            rgb_video_queue_.reset();
            left_video_queue_.reset();
            right_video_queue_.reset();
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
            
            // Restart publishing if we were using video
            if (use_video_) {
                setup_video_publisher();
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
        if (!rgb_video_queue_ || !left_video_queue_ || !right_video_queue_) {
            RCLCPP_ERROR(this->get_logger(), "Video queues are not initialized. Cannot setup video publisher.");
            return;
        }

        // Create publishers for RGB camera with sensor QoS profile
        std::string rgb_raw_topic = "/color/image";
        std::string rgb_compressed_topic = "/color/image/compressed";
        
        rgb_image_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
            rgb_raw_topic,
            rclcpp::SensorDataQoS()
        );
        
        rgb_compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
            rgb_compressed_topic,
            rclcpp::SensorDataQoS()
        );

        RCLCPP_INFO(this->get_logger(), "Created RGB camera publishers on topics: %s and %s", 
            rgb_raw_topic.c_str(), rgb_compressed_topic.c_str());

        // Create publishers for stereo cameras (only if enabled)
        if (enable_stereo_) {
            // Create publishers for left camera with sensor QoS profile
            std::string left_raw_topic = "/mono/left/image_raw";
            std::string left_compressed_topic = "/mono/left/image/compressed";
            
            left_image_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
                left_raw_topic,
                rclcpp::SensorDataQoS()
            );
            
            left_compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
                left_compressed_topic,
                rclcpp::SensorDataQoS()
            );

            RCLCPP_INFO(this->get_logger(), "Created left camera publishers on topics: %s and %s", 
                left_raw_topic.c_str(), left_compressed_topic.c_str());

            // Create publishers for right camera with sensor QoS profile
            std::string right_raw_topic = "/mono/right/image_raw";
            std::string right_compressed_topic = "/mono/right/image/compressed";
            
            right_image_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
                right_raw_topic,
                rclcpp::SensorDataQoS()
            );
            
            right_compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>(
                right_compressed_topic,
                rclcpp::SensorDataQoS()
            );

            RCLCPP_INFO(this->get_logger(), "Created right camera publishers on topics: %s and %s", 
                right_raw_topic.c_str(), right_compressed_topic.c_str());
        } else {
            RCLCPP_INFO(this->get_logger(), "Stereo publishers disabled - skipping stereo publisher creation");
        }

        // Create publishers for disparity image (only if disparity computation is enabled)
        if (enable_disparity_) {
            std::string disparity_raw_topic = "/stereo/disparity";
            disparity_pub_ = this->create_publisher<stereo_msgs::msg::DisparityImage>(
                disparity_raw_topic,
                rclcpp::SensorDataQoS()
            );
            RCLCPP_INFO(this->get_logger(), "Created disparity publisher on topic: %s", disparity_raw_topic.c_str());

            // Create publisher for disparity visualization (for rqt_image_view)
            std::string disparity_viz_topic = "/stereo/disparity/image";
            disparity_viz_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
                disparity_viz_topic,
                rclcpp::SensorDataQoS()
            );
            RCLCPP_INFO(this->get_logger(), "Created disparity visualization publisher on topic: %s", disparity_viz_topic.c_str());
        } else {
            RCLCPP_INFO(this->get_logger(), "Disparity computation disabled - skipping disparity publishers");
        }

        // Create timer for 30Hz publishing
        publish_timer_ = this->create_wall_timer(
            std::chrono::milliseconds(33),
            std::bind(&CameraDriverNode::publish_all_frames, this)
        );
    }

    void initialize_debug_directory() {
        try {
            // Handle tilde expansion for home directory
            std::string expanded_path = debug_output_dir_;
            if (expanded_path.find("~/") == 0) {
                const char* home_dir = std::getenv("HOME");
                if (home_dir) {
                    expanded_path = std::string(home_dir) + expanded_path.substr(1);
                }
            }
            
            std::filesystem::path debug_path(expanded_path);
            if (!std::filesystem::exists(debug_path)) {
                if (std::filesystem::create_directories(debug_path)) {
                    RCLCPP_INFO(this->get_logger(), "Created debug output directory: %s", expanded_path.c_str());
                } else {
                    RCLCPP_ERROR(this->get_logger(), "Failed to create debug output directory: %s", expanded_path.c_str());
                    debug_save_images_ = false; // Disable debug saving if directory creation fails
                }
            } else {
                RCLCPP_INFO(this->get_logger(), "Debug output directory exists: %s", expanded_path.c_str());
            }
            
            // Update the path to use the expanded version
            debug_output_dir_ = expanded_path;
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error initializing debug directory: %s", e.what());
            debug_save_images_ = false; // Disable debug saving if there's an error
        }
    }

    void compute_and_publish_disparity(const cv::Mat& left_image, const cv::Mat& right_image, const rclcpp::Time& stamp, int frame_count) {
        cv::Mat disparity_map;
        if (sgbm_) {
            sgbm_->compute(left_image, right_image, disparity_map);
        } else {
            RCLCPP_WARN(this->get_logger(), "StereoSGBM not initialized, skipping disparity computation.");
            return;
        }

        // Convert disparity to something viewable
        cv::Mat disparity_normalized;
        cv::normalize(disparity_map, disparity_normalized, 0, 255, cv::NORM_MINMAX, CV_8U);

        // Publish the disparity image (stereo_msgs/DisparityImage)
        stereo_msgs::msg::DisparityImage disparity_msg;
        disparity_msg.header.stamp = stamp;
        disparity_msg.header.frame_id = tf_prefix_ + "_left_camera_frame"; // Or your rectified frame
        
        disparity_msg.f = right_cam_info_->k[0]; // Focal length
        disparity_msg.t = -right_cam_info_->p[3] / disparity_msg.f; // Baseline

        sensor_msgs::msg::Image& image_msg = disparity_msg.image;
        image_msg.header = disparity_msg.header;
        image_msg.height = disparity_map.rows;
        image_msg.width = disparity_map.cols;
        image_msg.encoding = "32FC1";
        image_msg.step = disparity_map.cols * sizeof(float);
        image_msg.is_bigendian = false;
        
        std::vector<uint8_t> data_vec;
        data_vec.assign((char*)disparity_map.data, (char*)disparity_map.data + disparity_map.total() * disparity_map.elemSize());
        image_msg.data = data_vec;

        disparity_pub_->publish(disparity_msg);

        // Publish disparity visualization image (sensor_msgs/Image) for rqt_image_view
        sensor_msgs::msg::Image disparity_viz_msg;
        disparity_viz_msg.header.stamp = stamp;
        disparity_viz_msg.header.frame_id = tf_prefix_ + "_left_camera_frame";
        disparity_viz_msg.height = disparity_normalized.rows;
        disparity_viz_msg.width = disparity_normalized.cols;
        disparity_viz_msg.encoding = "mono8";
        disparity_viz_msg.is_bigendian = false;
        disparity_viz_msg.step = disparity_normalized.cols * 1;
        disparity_viz_msg.data = std::vector<uint8_t>(disparity_normalized.data, 
            disparity_normalized.data + disparity_normalized.total());

        disparity_viz_pub_->publish(disparity_viz_msg);

        // Save debug image
        if (debug_save_images_ && (frame_count % debug_save_interval_ == 0)) {
            try {
                cv::Mat disparity_colorized;
                cv::applyColorMap(disparity_normalized, disparity_colorized, cv::COLORMAP_JET);

                int file_index = (frame_count / debug_save_interval_) % 3;
                std::stringstream filename;
                filename << debug_output_dir_ << "/disparity_map_" << file_index << ".jpg";
                
                std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 95};
                if (cv::imwrite(filename.str(), disparity_colorized, params)) {
                    RCLCPP_DEBUG(this->get_logger(), "Saved debug disparity map: %s (frame %d)", filename.str().c_str(), frame_count);
                } else {
                    RCLCPP_ERROR(this->get_logger(), "Failed to save debug image: %s", filename.str().c_str());
                }
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Error saving debug image: %s", e.what());
            }
        }
    }

    void publish_all_frames() {
        try {
            // Get all frames
            auto rgb_frame = rgb_video_queue_->tryGet<dai::ImgFrame>();
            
            // Only fetch stereo frames if stereo is enabled
            std::shared_ptr<dai::ImgFrame> left_frame, right_frame;
            if (enable_stereo_) {
                left_frame = left_video_queue_->tryGet<dai::ImgFrame>();
                right_frame = right_video_queue_->tryGet<dai::ImgFrame>();
            }

            auto now = this->now();
            static int frame_count = 0;
            frame_count++;

            // Publish RGB frame if available
            if (rgb_frame) {
                auto rgb_img_data = rgb_frame->getData();
                if (!rgb_img_data.empty()) {
                    // Convert to OpenCV Mat
                    cv::Mat rgb_cvFrame(rgb_frame->getHeight(), rgb_frame->getWidth(), CV_8UC3, rgb_img_data.data());
                    
                    // Create and publish raw RGB image message
                    sensor_msgs::msg::Image rgb_ros_image;
                    rgb_ros_image.header.stamp = now;
                    rgb_ros_image.header.frame_id = tf_prefix_ + "_rgb_camera_optical_frame";
                    rgb_ros_image.height = rgb_frame->getHeight();
                    rgb_ros_image.width = rgb_frame->getWidth();
                    rgb_ros_image.encoding = "bgr8";
                    rgb_ros_image.is_bigendian = false;
                    rgb_ros_image.step = rgb_frame->getWidth() * 3;
                    rgb_ros_image.data = std::vector<uint8_t>(rgb_img_data.begin(), rgb_img_data.end());
                    rgb_image_pub_->publish(rgb_ros_image);

                    // Create and publish compressed RGB image message
                    sensor_msgs::msg::CompressedImage rgb_compressed_msg;
                    rgb_compressed_msg.header = rgb_ros_image.header;
                    rgb_compressed_msg.format = "jpeg";
                    std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, jpeg_quality_};
                    cv::imencode(".jpg", rgb_cvFrame, rgb_compressed_msg.data, params);
                    rgb_compressed_pub_->publish(rgb_compressed_msg);
                }
            }

            // Publish stereo frames if available and enabled
            if (enable_stereo_ && left_frame && right_frame) {
                auto left_img_data = left_frame->getData();
                auto right_img_data = right_frame->getData();

                if (!left_img_data.empty() && !right_img_data.empty()) {
                    // Convert to OpenCV Mat
                    cv::Mat left_cvFrame(left_frame->getHeight(), left_frame->getWidth(), CV_8UC1, left_img_data.data());
                    cv::Mat right_cvFrame(right_frame->getHeight(), right_frame->getWidth(), CV_8UC1, right_img_data.data());

                    // --- Publish Left Frame ---
                    sensor_msgs::msg::Image left_ros_image;
                    left_ros_image.header.stamp = now;
                    left_ros_image.header.frame_id = tf_prefix_ + "_left_camera_frame";
                    left_ros_image.height = left_frame->getHeight();
                    left_ros_image.width = left_frame->getWidth();
                    left_ros_image.encoding = "mono8";
                    left_ros_image.is_bigendian = false;
                    left_ros_image.step = left_frame->getWidth() * 1;
                    left_ros_image.data = std::vector<uint8_t>(left_img_data.begin(), left_img_data.end());
                    left_image_pub_->publish(left_ros_image);

                    if (publish_stereo_compressed_) {
                        sensor_msgs::msg::CompressedImage left_compressed_msg;
                        left_compressed_msg.header = left_ros_image.header;
                        left_compressed_msg.format = "jpeg";
                        std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, jpeg_quality_};
                        cv::imencode(".jpg", left_cvFrame, left_compressed_msg.data, params);
                        left_compressed_pub_->publish(left_compressed_msg);
                    }
                    
                    // --- Publish Right Frame ---
                    sensor_msgs::msg::Image right_ros_image;
                    right_ros_image.header.stamp = now;
                    right_ros_image.header.frame_id = tf_prefix_ + "_right_camera_frame";
                    right_ros_image.height = right_frame->getHeight();
                    right_ros_image.width = right_frame->getWidth();
                    right_ros_image.encoding = "mono8";
                    right_ros_image.is_bigendian = false;
                    right_ros_image.step = right_frame->getWidth() * 1;
                    right_ros_image.data = std::vector<uint8_t>(right_img_data.begin(), right_img_data.end());
                    right_image_pub_->publish(right_ros_image);

                    if (publish_stereo_compressed_) {
                        sensor_msgs::msg::CompressedImage right_compressed_msg;
                        right_compressed_msg.header = right_ros_image.header;
                        right_compressed_msg.format = "jpeg";
                        std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, jpeg_quality_};
                        cv::imencode(".jpg", right_cvFrame, right_compressed_msg.data, params);
                        right_compressed_pub_->publish(right_compressed_msg);
                    }

                    // Save debug stereo pair image if enabled
                    if (debug_save_images_ && (frame_count % debug_save_interval_ == 0)) {
                        try {
                            cv::Mat stereo_pair;
                            cv::hconcat(left_cvFrame, right_cvFrame, stereo_pair);

                            int file_index = (frame_count / debug_save_interval_) % 3;
                            std::stringstream filename;
                            filename << debug_output_dir_ << "/stereo_pair_" << file_index << ".jpg";
                            
                            std::vector<int> imwrite_params = {cv::IMWRITE_JPEG_QUALITY, 95};
                            if (cv::imwrite(filename.str(), stereo_pair, imwrite_params)) {
                                RCLCPP_DEBUG(this->get_logger(), "Saved debug stereo pair: %s (frame %d)", filename.str().c_str(), frame_count);
                            } else {
                                RCLCPP_ERROR(this->get_logger(), "Failed to save debug stereo pair: %s", filename.str().c_str());
                            }
                        } catch (const std::exception& e) {
                            RCLCPP_ERROR(this->get_logger(), "Error saving debug stereo pair: %s", e.what());
                        }
                    }

                    // --- Compute and Publish Disparity ---
                    // Only compute disparity every 5 frames to improve performance
                    if (frame_count % 5 == 0 && enable_disparity_) {
                        compute_and_publish_disparity(left_cvFrame, right_cvFrame, now, frame_count);
                    }
                }
            }

            // Log frame details periodically
            if (frame_count % 30 == 0) {
                RCLCPP_INFO(this->get_logger(), "Published frame %d", frame_count);
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
                            RCLCPP_FATAL(this->get_logger(), "Maximum restart attempts reached. Node will continue but may not function properly.");
                        } else {
                            RCLCPP_WARN(this->get_logger(), "Will retry restart in next communication error");
                        }
                    }
                } else {
                    RCLCPP_FATAL(this->get_logger(), "Maximum restart attempts reached. Node will continue but may not function properly.");
                }
            } else {
                RCLCPP_ERROR(this->get_logger(), "Non-communication error in publish_all_frames: %s", e.what());
            }
        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Unexpected error in publish_all_frames: %s", e.what());
        }
    }

    // RGB publishers
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr rgb_image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr rgb_compressed_pub_;
    
    // Stereo publishers
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr left_image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr left_compressed_pub_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr right_image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr right_compressed_pub_;
    rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr disparity_pub_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr disparity_viz_pub_;
    
    rclcpp::TimerBase::SharedPtr publish_timer_;

    std::string tf_prefix_;
    std::string camera_model_;
    bool use_video_;
    int stereo_confidence_threshold_;

    // Store parameters for device restart
    std::string color_resolution_str_;
    std::string resolution_str_;
    double fps_val_;
    std::string mxId_str_;
    bool usb2Mode_val_;

    // Debug parameters
    bool debug_save_images_;
    std::string debug_output_dir_;
    int debug_save_interval_;

    // Disparity parameters
    bool enable_disparity_;

    // Performance parameters
    int jpeg_quality_;
    bool publish_stereo_compressed_;
    double rgb_fps_;
    double stereo_fps_;
    bool enable_stereo_;

    cv::Ptr<cv::StereoSGBM> sgbm_;

    dai::Pipeline pipeline_;
    std::unique_ptr<dai::Device> device_;
    std::shared_ptr<dai::DataOutputQueue> rgb_video_queue_;
    std::shared_ptr<dai::DataOutputQueue> left_video_queue_;
    std::shared_ptr<dai::DataOutputQueue> right_video_queue_;
    
    dai::CalibrationHandler calibrationHandler_;
    std::unique_ptr<dai::rosBridge::ImageConverter> rgb_converter_;

    camera_info_manager::CameraInfoManager cinfo_manager_left_;
    camera_info_manager::CameraInfoManager cinfo_manager_right_;
    camera_info_manager::CameraInfoManager cinfo_manager_rgb_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> left_cam_info_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> right_cam_info_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> rgb_cam_info_;

    // Retry logic variables
    int retry_count_;
    int max_retries_;
    int retry_delay_ms_;
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<CameraDriverNode>();
    node->initialize_publishers(); // Call to initialize publishers after node is fully constructed
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
