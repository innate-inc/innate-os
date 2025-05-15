#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <linux/videodev2.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>  // For mmap, PROT_READ, PROT_WRITE, MAP_SHARED, MAP_FAILED
#include <string.h>
#include <chrono>

using namespace std::chrono_literals;

class CameraNode : public rclcpp::Node {
public:
    CameraNode() : Node("camera_node") {
        RCLCPP_INFO(this->get_logger(), "Initializing camera node...");
        RCLCPP_INFO(this->get_logger(), "OpenCV version: %s", CV_VERSION);

        // Parameters
        this->declare_parameter("device", "/dev/video0");
        this->declare_parameter("width", 640);
        this->declare_parameter("height", 480);
        this->declare_parameter("fps", 30);
        this->declare_parameter("pixel_format", "YUYV");

        // Get parameters
        device_path_ = this->get_parameter("device").as_string();
        width_ = this->get_parameter("width").as_int();
        height_ = this->get_parameter("height").as_int();
        fps_ = this->get_parameter("fps").as_int();

        RCLCPP_INFO(this->get_logger(), "Camera parameters:");
        RCLCPP_INFO(this->get_logger(), "  Device: %s", device_path_.c_str());
        RCLCPP_INFO(this->get_logger(), "  Resolution: %dx%d", width_, height_);
        RCLCPP_INFO(this->get_logger(), "  FPS: %d", fps_);
        RCLCPP_INFO(this->get_logger(), "  Pixel Format: YUYV");

        // Create publishers with sensor data QoS profile
        rclcpp::QoS qos = rclcpp::SensorDataQoS()
            .keep_last(10)  // Keep last 10 messages
            .best_effort()  // Best effort delivery for real-time data
            .durability_volatile();  // Volatile durability

        image_pub_ = this->create_publisher<sensor_msgs::msg::Image>("image_raw", qos);
        compressed_pub_ = this->create_publisher<sensor_msgs::msg::CompressedImage>("image_raw/compressed", qos);
        
        RCLCPP_INFO(this->get_logger(), "Created publishers with sensor data QoS:");
        RCLCPP_INFO(this->get_logger(), "  Raw image: /image_raw");
        RCLCPP_INFO(this->get_logger(), "  Compressed image: /image_raw/compressed");
        RCLCPP_INFO(this->get_logger(), "  QoS Settings:");
        RCLCPP_INFO(this->get_logger(), "    - Reliability: BEST_EFFORT");
        RCLCPP_INFO(this->get_logger(), "    - Durability: VOLATILE");
        RCLCPP_INFO(this->get_logger(), "    - History: KEEP_LAST (10)");
        RCLCPP_INFO(this->get_logger(), "    - Deadline: Default");
        RCLCPP_INFO(this->get_logger(), "    - Liveliness: Default");

        // Initialize camera
        if (!init_camera()) {
            RCLCPP_ERROR(this->get_logger(), "Failed to initialize camera");
            return;
        }

        RCLCPP_INFO(this->get_logger(), "Camera initialized successfully");

        // Create timer for frame capture
        timer_ = this->create_wall_timer(
            std::chrono::milliseconds(1000 / fps_),
            std::bind(&CameraNode::capture_and_publish, this));
        
        RCLCPP_INFO(this->get_logger(), "Camera node is ready");
    }

    ~CameraNode() {
        if (fd_ >= 0) {
            RCLCPP_INFO(this->get_logger(), "Closing camera device");
            close(fd_);
        }
    }

private:
    bool init_camera() {
        RCLCPP_INFO(this->get_logger(), "Opening camera device: %s", device_path_.c_str());
        fd_ = open(device_path_.c_str(), O_RDWR);
        if (fd_ < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to open camera device: %s", strerror(errno));
            return false;
        }

        struct v4l2_capability cap;
        if (ioctl(fd_, VIDIOC_QUERYCAP, &cap) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to query device capabilities");
            return false;
        }

        RCLCPP_INFO(this->get_logger(), "Camera capabilities:");
        RCLCPP_INFO(this->get_logger(), "  Driver: %s", cap.driver);
        RCLCPP_INFO(this->get_logger(), "  Card: %s", cap.card);
        RCLCPP_INFO(this->get_logger(), "  Bus info: %s", cap.bus_info);

        if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
            RCLCPP_ERROR(this->get_logger(), "Device does not support video capture");
            return false;
        }

        struct v4l2_format fmt = {};
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width = width_;
        fmt.fmt.pix.height = height_;
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUYV;
        fmt.fmt.pix.field = V4L2_FIELD_NONE;

        if (ioctl(fd_, VIDIOC_S_FMT, &fmt) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to set format");
            return false;
        }

        RCLCPP_INFO(this->get_logger(), "Set camera format:");
        RCLCPP_INFO(this->get_logger(), "  Width: %d", fmt.fmt.pix.width);
        RCLCPP_INFO(this->get_logger(), "  Height: %d", fmt.fmt.pix.height);
        RCLCPP_INFO(this->get_logger(), "  Pixel Format: YUYV");

        struct v4l2_streamparm streamparm = {};
        streamparm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        streamparm.parm.capture.timeperframe.numerator = 1;
        streamparm.parm.capture.timeperframe.denominator = fps_;

        if (ioctl(fd_, VIDIOC_S_PARM, &streamparm) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to set frame rate");
            return false;
        }

        RCLCPP_INFO(this->get_logger(), "Set frame rate: %d fps", fps_);

        // Request buffers
        struct v4l2_requestbuffers req = {};
        req.count = 4;
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        req.memory = V4L2_MEMORY_MMAP;

        if (ioctl(fd_, VIDIOC_REQBUFS, &req) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to request buffers");
            return false;
        }

        RCLCPP_INFO(this->get_logger(), "Requested %d buffers", req.count);

        // Map buffers
        for (unsigned int i = 0; i < req.count; i++) {
            struct v4l2_buffer buf = {};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;

            if (ioctl(fd_, VIDIOC_QUERYBUF, &buf) < 0) {
                RCLCPP_ERROR(this->get_logger(), "Failed to query buffer %d", i);
                return false;
            }

            buffers_[i].start = mmap(nullptr, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, buf.m.offset);
            if (buffers_[i].start == MAP_FAILED) {
                RCLCPP_ERROR(this->get_logger(), "Failed to map buffer %d", i);
                return false;
            }
            buffers_[i].length = buf.length;
            RCLCPP_INFO(this->get_logger(), "Mapped buffer %d: %u bytes", i, static_cast<unsigned int>(buf.length));
        }

        // Queue buffers
        for (unsigned int i = 0; i < req.count; i++) {
            struct v4l2_buffer buf = {};
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;

            if (ioctl(fd_, VIDIOC_QBUF, &buf) < 0) {
                RCLCPP_ERROR(this->get_logger(), "Failed to queue buffer %d", i);
                return false;
            }
        }

        // Start streaming
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (ioctl(fd_, VIDIOC_STREAMON, &type) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to start streaming");
            return false;
        }

        RCLCPP_INFO(this->get_logger(), "Camera streaming started");
        return true;
    }

    void capture_and_publish() {
        struct v4l2_buffer buf = {};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;

        if (ioctl(fd_, VIDIOC_DQBUF, &buf) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to dequeue buffer");
            return;
        }

        // Convert YUYV to BGR
        cv::Mat yuyv(height_, width_, CV_8UC2, buffers_[buf.index].start);
        cv::Mat bgr;
        cv::cvtColor(yuyv, bgr, cv::COLOR_YUV2BGR_YUYV);

        // Create and publish raw image message
        auto img_msg = cv_bridge::CvImage(std_msgs::msg::Header(), "bgr8", bgr).toImageMsg();
        img_msg->header.stamp = this->now();
        img_msg->header.frame_id = "camera";
        image_pub_->publish(*img_msg);

        // Create and publish compressed image message
        sensor_msgs::msg::CompressedImage compressed_msg;
        compressed_msg.header = img_msg->header;
        compressed_msg.format = "jpeg";

        // Compress the image using OpenCV with explicit quality settings
        std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, 80};  // 80% quality
        cv::imencode(".jpg", bgr, compressed_msg.data, params);
        compressed_pub_->publish(compressed_msg);

        // Requeue the buffer
        if (ioctl(fd_, VIDIOC_QBUF, &buf) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to requeue buffer");
        }
    }

    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
    rclcpp::TimerBase::SharedPtr timer_;

    int fd_ = -1;
    std::string device_path_;
    int width_;
    int height_;
    int fps_;

    struct Buffer {
        void* start;
        size_t length;
    } buffers_[4];
};

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<CameraNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
