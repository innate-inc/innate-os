#include <chrono>
#include <cstdio>
#include <functional>
#include <iostream>
#include <tuple>
#include <string>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "camera_info_manager/camera_info_manager.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/camera_info.hpp"

// DepthAI specific includes
#include "depthai/device/DataQueue.hpp"
#include "depthai/device/Device.hpp"
#include "depthai/pipeline/Pipeline.hpp"
#include "depthai/pipeline/node/ColorCamera.hpp"
#include "depthai/pipeline/node/XLinkOut.hpp"
#include "depthai_bridge/BridgePublisher.hpp"
#include "depthai_bridge/ImageConverter.hpp"

using namespace std::chrono_literals;

// Structure to hold resolution dimensions
struct ImageDimensions {
    int width;
    int height;
};

// Creates the DepthAI pipeline for RGB camera output
std::tuple<dai::Pipeline, ImageDimensions> create_rgb_pipeline(
    std::string color_resolution_str,
    float fps,
    bool use_preview,
    int preview_width,
    int preview_height) {

    dai::Pipeline pipeline;
    auto colorCam = pipeline.create<dai::node::ColorCamera>();
    auto xlinkOutVideo = pipeline.create<dai::node::XLinkOut>();

    xlinkOutVideo->setStreamName("rgb_video");

    dai::ColorCameraProperties::SensorResolution dai_color_resolution;
    ImageDimensions video_dimensions;

    if (color_resolution_str == "800p") {
        dai_color_resolution = dai::ColorCameraProperties::SensorResolution::THE_800_P;
        video_dimensions = {1280, 800};
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting color resolution to 800P (1280x800)");
    } else if (color_resolution_str == "720p") {
        dai_color_resolution = dai::ColorCameraProperties::SensorResolution::THE_720_P;
        video_dimensions = {1280, 720};
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Setting color resolution to 720P (1280x720)");
    } else {
        RCLCPP_ERROR(rclcpp::get_logger("rclcpp"), "Invalid color_resolution parameter: %s. Supported: 800p, 720p.", color_resolution_str.c_str());
        throw std::runtime_error("Invalid color camera resolution provided to pipeline creation.");
    }

    colorCam->setBoardSocket(dai::CameraBoardSocket::CAM_A); // RGB camera is typically CAM_A
    colorCam->setResolution(dai_color_resolution);
    colorCam->setVideoSize(video_dimensions.width, video_dimensions.height);
    colorCam->setInterleaved(false);
    colorCam->setColorOrder(dai::ColorCameraProperties::ColorOrder::BGR); // Common for depthai_bridge
    colorCam->setFps(fps);

    colorCam->video.link(xlinkOutVideo->input);

    if (use_preview) {
        auto xlinkOutPreview = pipeline.create<dai::node::XLinkOut>();
        xlinkOutPreview->setStreamName("rgb_preview");
        colorCam->setPreviewSize(preview_width, preview_height);
        colorCam->preview.link(xlinkOutPreview->input);
        RCLCPP_INFO(rclcpp::get_logger("rclcpp"), "Preview stream enabled with size: %dx%d", preview_width, preview_height);
    }

    return std::make_tuple(pipeline, video_dimensions);
}

class CameraDriverNode : public rclcpp::Node {
public:
    CameraDriverNode() : Node("camera_driver"),
                         cinfo_manager_(this) {
        // Declare parameters
        this->declare_parameter<std::string>("tf_prefix", "oak");
        this->declare_parameter<std::string>("camera_model", "OAK-D"); // For CameraInfoManager
        this->declare_parameter<std::string>("color_resolution", "800p");
        this->declare_parameter<double>("fps", 30.0);
        this->declare_parameter<bool>("use_video", true);
        this->declare_parameter<bool>("use_preview", false);
        this->declare_parameter<int>("preview_width", 300);
        this->declare_parameter<int>("preview_height", 300);
        // Device specific parameters
        this->declare_parameter<std::string>("mxId", "");
        this->declare_parameter<bool>("usb2Mode", false);
        // poeMode is typically handled by device detection or not explicitly set for basic RGB
        // Removed IR Emitter parameters

        // Get parameters
        tf_prefix_ = this->get_parameter("tf_prefix").as_string();
        camera_model_ = this->get_parameter("camera_model").as_string();
        std::string color_resolution_str = this->get_parameter("color_resolution").as_string();
        double fps_val = this->get_parameter("fps").as_double();
        use_video_ = this->get_parameter("use_video").as_bool();
        use_preview_ = this->get_parameter("use_preview").as_bool();
        preview_width_val_ = this->get_parameter("preview_width").as_int();
        preview_height_val_ = this->get_parameter("preview_height").as_int();
        
        std::string mxId_str = this->get_parameter("mxId").as_string();
        bool usb2Mode_val = this->get_parameter("usb2Mode").as_bool();

        RCLCPP_INFO(this->get_logger(), "Initializing Camera Driver Node with parameters:");
        RCLCPP_INFO(this->get_logger(), "  TF Prefix: %s", tf_prefix_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Camera Model: %s", camera_model_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Color Resolution: %s", color_resolution_str.c_str());
        RCLCPP_INFO(this->get_logger(), "  FPS: %.2f", fps_val);
        RCLCPP_INFO(this->get_logger(), "  Use Video: %s", use_video_ ? "true" : "false");
        RCLCPP_INFO(this->get_logger(), "  Use Preview: %s", use_preview_ ? "true" : "false");
        if(use_preview_){
            RCLCPP_INFO(this->get_logger(), "  Preview Size: %dx%d", preview_width_val_, preview_height_val_);
        }

        // Now that tf_prefix_ is available, initialize rgb_converter_
        rgb_converter_ = std::make_unique<dai::rosBridge::ImageConverter>(tf_prefix_ + "_rgb_camera_optical_frame", false);

        ImageDimensions video_dims;
        std::tie(pipeline_, video_dims) = create_rgb_pipeline(
            color_resolution_str, fps_val, use_preview_, preview_width_val_, preview_height_val_);

        // Initialize device
        dai::DeviceInfo deviceInfo; // Default constructor for first available device
        bool deviceFound = false;
        if (!mxId_str.empty()) {
            RCLCPP_INFO(this->get_logger(), "Attempting to find device with MXID: %s", mxId_str.c_str());
            try {
                dai::DeviceInfo di(mxId_str); // Try to find by MXID
                deviceInfo = di;
                deviceFound = true;
                 RCLCPP_INFO(this->get_logger(), "Device %s found.", mxId_str.c_str());
            } catch (const std::exception& e) {
                 RCLCPP_WARN(this->get_logger(), "Device with MXID %s not found or error: %s. Will try first available.", mxId_str.c_str(), e.what());
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

        device_ = std::make_unique<dai::Device>(pipeline_, deviceInfo, usb2Mode_val);

        // IR emitter setup removed as it's for Pro models

        // Get output queues
        if (use_video_) {
            video_queue_ = device_->getOutputQueue("rgb_video", 8, false);
        }
        if (use_preview_) {
            preview_queue_ = device_->getOutputQueue("rgb_preview", 8, false);
        }

        // Calibration and CameraInfo
        calibrationHandler_ = device_->readCalibration();
        
        std::string rgb_camera_name = tf_prefix_;
        cinfo_manager_.setCameraName(rgb_camera_name + "_rgb_camera");
        
        rgb_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
            rgb_converter_->calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_A, video_dims.width, video_dims.height)
        );

        // Publishers are initialized after construction via initialize_publishers()
        RCLCPP_INFO(this->get_logger(), "Camera driver node core initialized. Publisher setup deferred.");
    }

    void initialize_publishers() {
        if (use_video_) {
            setup_video_publisher();
        }

        if (use_preview_) {
            setup_preview_publisher();
        }
        RCLCPP_INFO(this->get_logger(), "Camera publishers initialized successfully.");
    }

private:
    void setup_video_publisher() {
        if (!video_queue_) {
            RCLCPP_ERROR(this->get_logger(), "Video queue is not initialized. Cannot setup video publisher.");
            return;
        }
        if (!rgb_cam_info_) {
            RCLCPP_ERROR(this->get_logger(), "RGB CameraInfo is not initialized. Cannot setup video publisher.");
            return;
        }
        rgb_publisher_ = std::make_unique<dai::rosBridge::BridgePublisher<sensor_msgs::msg::Image, dai::ImgFrame>>(
            video_queue_,
            shared_from_this(),
            std::string(tf_prefix_ + "/color/image_raw"),
            std::bind(&dai::rosBridge::ImageConverter::toRosMsg,
                        rgb_converter_.get(),
                        std::placeholders::_1,
                        std::placeholders::_2),
            30,
            *rgb_cam_info_,
            tf_prefix_ + "/color"
        );
        rgb_publisher_->addPublisherCallback();
        RCLCPP_INFO(this->get_logger(), "RGB video stream will be published on: %s", (tf_prefix_ + "/color/image_raw").c_str());
        RCLCPP_INFO(this->get_logger(), "RGB CameraInfo will be published on: %s", (tf_prefix_ + "/color/camera_info").c_str());
    }

    void setup_preview_publisher() {
        if (!preview_queue_) {
            RCLCPP_ERROR(this->get_logger(), "Preview queue is not initialized. Cannot setup preview publisher.");
            return;
        }
        // Create CameraInfo for preview
        preview_cam_info_ = std::make_shared<sensor_msgs::msg::CameraInfo>(
            rgb_converter_->calibrationToCameraInfo(calibrationHandler_, dai::CameraBoardSocket::CAM_A, preview_width_val_, preview_height_val_)
        );
         if (!preview_cam_info_) {
            RCLCPP_ERROR(this->get_logger(), "Preview CameraInfo is not initialized. Cannot setup preview publisher.");
            return;
        }

        preview_publisher_ = std::make_unique<dai::rosBridge::BridgePublisher<sensor_msgs::msg::Image, dai::ImgFrame>>(
            preview_queue_,
            shared_from_this(),
            std::string(tf_prefix_ + "/color/preview/image_raw"),
            std::bind(&dai::rosBridge::ImageConverter::toRosMsg,
                        rgb_converter_.get(),
                        std::placeholders::_1,
                        std::placeholders::_2),
            30,
            *preview_cam_info_,
            tf_prefix_ + "/color/preview"
        );
        preview_publisher_->addPublisherCallback();
        RCLCPP_INFO(this->get_logger(), "RGB preview stream will be published on: %s", (tf_prefix_ + "/color/preview/image_raw").c_str());
    }

    std::string tf_prefix_;
    std::string camera_model_;
    bool use_video_;
    bool use_preview_;
    int preview_width_val_;
    int preview_height_val_;

    dai::Pipeline pipeline_;
    std::unique_ptr<dai::Device> device_;
    std::shared_ptr<dai::DataOutputQueue> video_queue_;
    std::shared_ptr<dai::DataOutputQueue> preview_queue_;
    
    dai::CalibrationHandler calibrationHandler_;
    std::unique_ptr<dai::rosBridge::ImageConverter> rgb_converter_;

    camera_info_manager::CameraInfoManager cinfo_manager_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> rgb_cam_info_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> preview_cam_info_;

    std::unique_ptr<dai::rosBridge::BridgePublisher<sensor_msgs::msg::Image, dai::ImgFrame>> rgb_publisher_;
    std::unique_ptr<dai::rosBridge::BridgePublisher<sensor_msgs::msg::Image, dai::ImgFrame>> preview_publisher_;
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<CameraDriverNode>();
    node->initialize_publishers(); // Call to initialize publishers after node is fully constructed
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
