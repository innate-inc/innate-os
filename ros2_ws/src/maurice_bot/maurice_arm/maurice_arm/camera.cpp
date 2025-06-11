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
#include <thread>
#include <sys/select.h>  // Add for select()
#include <sys/stat.h>    // Add for stat()

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
    bool device_exists() {
        struct stat st;
        return (stat(device_path_.c_str(), &st) == 0);
    }

    bool device_responsive() {
        if (fd_ < 0) return false;
        
        // Try to query device capabilities - this should fail if device is gone
        struct v4l2_capability cap;
        if (ioctl(fd_, VIDIOC_QUERYCAP, &cap) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Device health check failed: %s", strerror(errno));
            return false;
        }
        return true;
    }

    bool init_camera() {
        RCLCPP_INFO(this->get_logger(), "Opening camera device: %s", device_path_.c_str());
        
        // Check if device file exists first
        if (!device_exists()) {
            RCLCPP_ERROR(this->get_logger(), "Device file does not exist: %s", device_path_.c_str());
            return false;
        }

        fd_ = open(device_path_.c_str(), O_RDWR | O_NONBLOCK);  // Add O_NONBLOCK
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

    void handle_stream_error() {
        RCLCPP_ERROR(this->get_logger(), "Stream error occurred (errno=%d: %s), attempting to recover...", errno, strerror(errno));
        
        // Check if this looks like a device disconnection
        if (errno == ENODEV || errno == ENOENT || errno == EPIPE || errno == EIO || errno == EAGAIN) {
            RCLCPP_ERROR(this->get_logger(), "Device appears to be disconnected, attempting full reconnection...");
            
            // Unmap buffers first
            for (int i = 0; i < 4; i++) {
                if (buffers_[i].start && buffers_[i].start != MAP_FAILED) {
                    munmap(buffers_[i].start, buffers_[i].length);
                    buffers_[i].start = nullptr;
                    buffers_[i].length = 0;
                }
            }
            
            // Close current connection
            if (fd_ >= 0) {
                close(fd_);
                fd_ = -1;
            }
            
            // Wait a bit for device to stabilize
            std::this_thread::sleep_for(std::chrono::milliseconds(2000));
            
            // Try to reinitialize camera
            if (init_camera()) {
                RCLCPP_INFO(this->get_logger(), "Successfully reconnected to camera");
            } else {
                RCLCPP_ERROR(this->get_logger(), "Failed to reconnect to camera");
            }
            return;
        }
        
        // For other errors, try simple stream restart
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (ioctl(fd_, VIDIOC_STREAMOFF, &type) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to stop streaming: %s", strerror(errno));
        }
        
        // Restart streaming
        if (ioctl(fd_, VIDIOC_STREAMON, &type) < 0) {
            RCLCPP_ERROR(this->get_logger(), "Failed to restart streaming: %s", strerror(errno));
        } else {
            RCLCPP_INFO(this->get_logger(), "Successfully restarted streaming");
        }
    }

    void capture_and_publish() {
        // Check if camera is still connected before attempting capture
        if (fd_ < 0) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "Camera not connected, attempting reconnection...");
            
            // Try to reconnect
            if (device_exists() && init_camera()) {
                RCLCPP_INFO(this->get_logger(), "Camera reconnected successfully");
            }
            return;
        }

        // Check if device file still exists
        if (!device_exists()) {
            RCLCPP_ERROR(this->get_logger(), "Device file disappeared: %s", device_path_.c_str());
            handle_stream_error();
            return;
        }

        // Use select to check if data is available (with timeout)
        fd_set fds;
        struct timeval tv;
        FD_ZERO(&fds);
        FD_SET(fd_, &fds);
        tv.tv_sec = 0;
        tv.tv_usec = 100000;  // 100ms timeout

        int ret = select(fd_ + 1, &fds, nullptr, nullptr, &tv);
        if (ret < 0) {
            RCLCPP_ERROR(this->get_logger(), "select() failed: %s", strerror(errno));
            handle_stream_error();
            return;
        } else if (ret == 0) {
            // Timeout - no data available, this might indicate a problem
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "No data available from camera (timeout)");
            
            // Check if device is still responsive
            if (!device_responsive()) {
                RCLCPP_ERROR(this->get_logger(), "Device no longer responsive");
                handle_stream_error();
            }
            return;
        }

        struct v4l2_buffer buf = {};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;

        if (ioctl(fd_, VIDIOC_DQBUF, &buf) < 0) {
            RCLCPP_ERROR(this->get_logger(),
                         "VIDIOC_DQBUF failed (idx=%u, errno=%d): %s", buf.index, errno, strerror(errno));
            handle_stream_error();
            return;
        }

        // Validate buffer index
        if (buf.index >= 4) {
            RCLCPP_ERROR(this->get_logger(), "Invalid buffer index: %u", buf.index);
            return;
        }

        // Check if buffer data looks valid
        if (!buffers_[buf.index].start || buf.bytesused == 0) {
            RCLCPP_ERROR(this->get_logger(), "Invalid buffer data (idx=%u, bytesused=%u)", buf.index, buf.bytesused);
            // Still try to requeue the buffer
            if (ioctl(fd_, VIDIOC_QBUF, &buf) < 0) {
                RCLCPP_ERROR(this->get_logger(), "Failed to requeue invalid buffer");
                handle_stream_error();
            }
            return;
        }

        try {
            // Convert YUYV to BGR
            cv::Mat yuyv(height_, width_, CV_8UC2, buffers_[buf.index].start);
            cv::Mat bgr;
            cv::cvtColor(yuyv, bgr, cv::COLOR_YUV2BGR_YUYV);

            // Validate converted image
            if (bgr.empty() || bgr.cols != width_ || bgr.rows != height_) {
                RCLCPP_ERROR(this->get_logger(), "Invalid converted image (idx=%u)", buf.index);
                // Don't return here, still need to requeue buffer
            } else {
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
            }
        } catch (const cv::Exception &e) {
            RCLCPP_ERROR(this->get_logger(),
                         "OpenCV exception on buffer %u: %s", buf.index, e.what());
            handle_stream_error();
            return;
        }

        // Requeue the buffer
        if (ioctl(fd_, VIDIOC_QBUF, &buf) < 0) {
            RCLCPP_ERROR(this->get_logger(),
                         "VIDIOC_QBUF failed (idx=%u, errno=%d): %s", buf.index, errno, strerror(errno));
            handle_stream_error();
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
