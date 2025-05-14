#include <chrono>
#include <cstdio>
#include <functional>
#include <iostream>
#include <tuple>
#include <string>
#include <vector>
#include <deque>

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
                         cinfo_manager_(this) {
        // Declare parameters
        this->declare_parameter<std::string>("tf_prefix", "oak");
        this->declare_parameter<std::string>("camera_model", "OAK-D");
        this->declare_parameter<std::string>("color_resolution", "800p");
        this->declare_parameter<double>("fps", 30.0);
        this->declare_parameter<bool>("use_video", true);
        // Device specific parameters
        this->declare_parameter<std::string>("mxId", "");
        this->declare_parameter<bool>("usb2Mode", false);

        // Get parameters
        tf_prefix_ = this->get_parameter("tf_prefix").as_string();
        camera_model_ = this->get_parameter("camera_model").as_string();
        std::string color_resolution_str = this->get_parameter("color_resolution").as_string();
        double fps_val = this->get_parameter("fps").as_double();
        use_video_ = this->get_parameter("use_video").as_bool();
        
        std::string mxId_str = this->get_parameter("mxId").as_string();
        bool usb2Mode_val = this->get_parameter("usb2Mode").as_bool();

        RCLCPP_INFO(this->get_logger(), "Initializing Camera Driver Node with parameters:");
        RCLCPP_INFO(this->get_logger(), "  TF Prefix: %s", tf_prefix_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Camera Model: %s", camera_model_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Color Resolution: %s", color_resolution_str.c_str());
        RCLCPP_INFO(this->get_logger(), "  FPS: %.2f", fps_val);
        RCLCPP_INFO(this->get_logger(), "  Use Video: %s", use_video_ ? "true" : "false");

        // Now that tf_prefix_ is available, initialize rgb_converter_
        rgb_converter_ = std::make_unique<dai::rosBridge::ImageConverter>(tf_prefix_ + "_rgb_camera_optical_frame", false);

        ImageDimensions video_dims;
        std::tie(pipeline_, video_dims) = create_rgb_pipeline(
            color_resolution_str, fps_val);

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
        RCLCPP_INFO(this->get_logger(), "Camera publishers initialized successfully.");
    }

private:
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

        // Create timer for 30Hz publishing
        publish_timer_ = this->create_wall_timer(
            std::chrono::milliseconds(33),
            std::bind(&CameraDriverNode::publish_frame, this)
        );
    }

    void publish_frame() {
        auto frame = video_queue_->get<dai::ImgFrame>();
        if (frame) {
            try {
                auto imgData = frame->getData();
                if (!imgData.empty()) {
                    // Convert to OpenCV Mat
                    cv::Mat cvFrame(frame->getHeight(), frame->getWidth(), CV_8UC3, imgData.data());
                    
                    // Create and publish raw image message
                    sensor_msgs::msg::Image rosImage;
                    rosImage.header.stamp = this->now();
                    rosImage.header.frame_id = tf_prefix_ + "_rgb_camera_optical_frame";
                    rosImage.height = frame->getHeight();
                    rosImage.width = frame->getWidth();
                    rosImage.encoding = "bgr8";
                    rosImage.is_bigendian = false;
                    rosImage.step = frame->getWidth() * 3;
                    rosImage.data = std::vector<uint8_t>(imgData.begin(), imgData.end());
                    image_pub_->publish(rosImage);

                    // Create and publish compressed image message
                    sensor_msgs::msg::CompressedImage compressed_msg;
                    compressed_msg.header = rosImage.header;
                    compressed_msg.format = "jpeg";
                    
                    // Compress the image using OpenCV
                    std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 80};  // 80% quality
                    cv::imencode(".jpg", cvFrame, compressed_msg.data, params);
                    
                    compressed_pub_->publish(compressed_msg);

                    // Log frame details periodically
                    static int frame_count = 0;
                    if (++frame_count % 30 == 0) {
                        RCLCPP_INFO(this->get_logger(), "Published frame %d - Raw size: %zu bytes, Compressed size: %zu bytes",
                            frame_count, rosImage.data.size(), compressed_msg.data.size());
                    }
                }
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Failed to publish frame: %s", e.what());
            }
        }
    }

    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
    rclcpp::TimerBase::SharedPtr publish_timer_;

    std::string tf_prefix_;
    std::string camera_model_;
    bool use_video_;

    dai::Pipeline pipeline_;
    std::unique_ptr<dai::Device> device_;
    std::shared_ptr<dai::DataOutputQueue> video_queue_;
    
    dai::CalibrationHandler calibrationHandler_;
    std::unique_ptr<dai::rosBridge::ImageConverter> rgb_converter_;

    camera_info_manager::CameraInfoManager cinfo_manager_;
    std::shared_ptr<sensor_msgs::msg::CameraInfo> rgb_cam_info_;
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<CameraDriverNode>();
    node->initialize_publishers(); // Call to initialize publishers after node is fully constructed
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
