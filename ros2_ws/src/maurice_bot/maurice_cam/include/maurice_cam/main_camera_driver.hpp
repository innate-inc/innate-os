#pragma once

#include <chrono>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <atomic>
#include <deque>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <cstring>
#include <stdexcept>

#include <turbojpeg.h>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_components/register_node_macro.hpp"
#include "sensor_msgs/msg/image.hpp"
#include "sensor_msgs/msg/compressed_image.hpp"
#include "sensor_msgs/msg/camera_info.hpp"

#include "maurice_cam/stereo_calibration.hpp"

#include <opencv2/opencv.hpp>

namespace maurice_cam
{

/**
 * @brief Hardware-accelerated JPEG encoder using libturbojpeg
 * 
 * Provides faster JPEG encoding compared to cv::imencode by using
 * libjpeg-turbo's optimized SIMD routines.
 */
class JpegTurboEncoder {
public:
  JpegTurboEncoder() {
    handle_ = tjInitCompress();
    if (!handle_) throw std::runtime_error("tjInitCompress failed");
  }

  ~JpegTurboEncoder() {
    if (handle_) tjDestroy(handle_);
  }

  // Non-copyable
  JpegTurboEncoder(const JpegTurboEncoder&) = delete;
  JpegTurboEncoder& operator=(const JpegTurboEncoder&) = delete;

  /**
   * @brief Encode BGR8 cv::Mat into JPEG
   * @param bgr Input BGR image (CV_8UC3)
   * @param quality JPEG quality (1-100)
   * @param out Output buffer for compressed JPEG data
   */
  void encodeBGR(const cv::Mat& bgr, int quality, std::vector<uint8_t>& out) {
    if (bgr.empty()) throw std::runtime_error("encodeBGR: empty image");
    if (bgr.type() != CV_8UC3) throw std::runtime_error("encodeBGR: expected CV_8UC3 (BGR)");
    
    if (!bgr.isContinuous()) {
      // Make it continuous (rare case)
      cv::Mat tmp = bgr.clone();
      encodeBGR(tmp, quality, out);
      return;
    }

    unsigned char* jpegBuf = nullptr;
    unsigned long jpegSize = 0;

    // TJSAMP_420 is fastest/most common; TJFLAG_FASTDCT prioritizes speed
    int flags = TJFLAG_FASTDCT;
    int rc = tjCompress2(
      handle_,
      bgr.data,
      bgr.cols,
      0,                // pitch 0 => infer from width * pixelSize (works because continuous)
      bgr.rows,
      TJPF_BGR,
      &jpegBuf,
      &jpegSize,
      TJSAMP_420,
      quality,
      flags
    );

    if (rc != 0) {
      const char* err = tjGetErrorStr2(handle_);
      throw std::runtime_error(std::string("tjCompress2 failed: ") + (err ? err : ""));
    }

    out.assign(jpegBuf, jpegBuf + jpegSize);
    tjFree(jpegBuf);
  }

private:
  tjhandle handle_{nullptr};
};

/**
 * @brief PID-based auto exposure controller with center weighting
 */
class AutoExposureController
{
public:
  AutoExposureController();
  
  /**
   * @brief Initialize the controller with exposure limits and proportional gain
   * @param min_exposure Minimum exposure value
   * @param max_exposure Maximum exposure value
   * @param target_brightness Target brightness (0-255)
   * @param kp Proportional gain
   */
  void initialize(int min_exposure, int max_exposure, double target_brightness = 128.0,
                 double kp = 0.8);
  
  /**
   * @brief Calculate new exposure value based on frame brightness
   * @param frame Input frame
   * @param current_exposure Current exposure value
   * @return New exposure value
   */
  int calculateExposure(const cv::Mat& frame, int current_exposure);
  
  /**
   * @brief Update proportional gain
   */
  void setPID(double kp);
  
  /**
   * @brief Update target brightness
   */
  void setTargetBrightness(double target);

private:
  /**
   * @brief Calculate center-weighted brightness of the frame
   * @param frame Input frame
   * @return Center-weighted brightness (0-255)
   */
  double calculateCenterWeightedBrightness(const cv::Mat& frame);
  
  // Proportional controller parameters
  double kp_;
  double target_brightness_;
  
  // Exposure limits
  int min_exposure_, max_exposure_;
  
  // Center weighting parameters
  double center_weight_;  // Weight for center region (0.0-1.0)
  double center_region_size_;  // Size of center region (0.0-1.0)
};

/**
 * @brief GStreamer-based main camera driver node for Maurice robot
 * 
 * This node provides a clean interface to capture frames from the main USB camera
 * using GStreamer pipeline and publishes both raw and compressed images.
 */
class MainCameraDriver : public rclcpp::Node
{
public:
  /**
   * @brief Auto exposure mode selection
   */
  enum class AutoExposureMode : int {
    HARDWARE = 0,   // Camera built-in AE (aperture priority)
    CUSTOM_PID = 1, // Custom PID controller (manual V4L2 mode)
    MANUAL = 2,     // Pure manual, no AE
  };

  /**
   * @brief Constructor
   * @param options Node options for component composition
   */
  explicit MainCameraDriver(const rclcpp::NodeOptions & options = rclcpp::NodeOptions());

  /**
   * @brief Destructor
   */
  ~MainCameraDriver();

private:
  /**
   * @brief Initialize camera with GStreamer pipeline
   * @return true if successful, false otherwise
   */
  bool initializeCamera();

  /**
   * @brief Create GStreamer pipeline string
   * @return Pipeline string for camera capture
   */
  std::string createGStreamerPipeline();

  /**
   * @brief Initialize V4L2 controls for manual exposure/gain control
   * @return true if successful, false otherwise
   */
  bool initializeV4L2Controls();

  /**
   * @brief Set a V4L2 control value
   * @param control_id V4L2 control ID
   * @param value Value to set
   * @return true if successful, false otherwise
   */
  bool setV4L2Control(int control_id, int value);

  /**
   * @brief Get a V4L2 control value
   * @param control_id V4L2 control ID
   * @return Control value, or -1 if failed
   */
  int getV4L2Control(int control_id);

  /**
   * @brief Main frame processing loop
   */
  void frameProcessingLoop();

  /**
   * @brief Update frame statistics
   */
  void updateFrameStats();

  /**
   * @brief Print frame statistics
   */
  void printFrameStats();

  /**
   * @brief Process captured frame and publish messages
   * @param frame Captured frame from camera
   */
  void processAndPublishFrame(const cv::Mat& frame);

  /**
   * @brief Apply auto exposure control to frame
   * @param frame Input frame for brightness analysis
   */
  void applyAutoExposure(const cv::Mat& frame);

  /**
   * @brief Timer callback: check if stereo calibration file changed and reload.
   */
  void checkCalibrationFile();

  // ROS 2 publishers
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr left_pub_;   // Left camera raw
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr right_pub_;  // Right camera raw
  rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr stereo_pub_;

  // Camera info publishers + calibration
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr left_info_pub_;
  rclcpp::Publisher<sensor_msgs::msg::CameraInfo>::SharedPtr right_info_pub_;
  std::shared_ptr<StereoCalibration> stereo_calib_;
  sensor_msgs::msg::CameraInfo left_info_msg_;
  sensor_msgs::msg::CameraInfo right_info_msg_;
  bool calibration_loaded_{false};
  std::mutex calib_mutex_;
  rclcpp::TimerBase::SharedPtr calib_watch_timer_;
  std::filesystem::file_time_type calib_last_write_;

  // Frame processing thread
  std::thread frame_thread_;
  std::atomic<bool> frame_thread_running_{false};

  // Camera parameters
  std::string data_directory_;
  std::string camera_device_;
  int capture_width_;      // Capture resolution (full FOV)
  int capture_height_;
  int left_width_;         // Left camera at capture resolution
  int left_height_;
  int publish_left_width_;   // Publish left at this resolution
  int publish_left_height_;
  int publish_stereo_width_; // Publish stereo at this resolution
  int publish_stereo_height_;
  double fps_;
  std::string frame_id_;
  std::string right_frame_id_;
  int jpeg_quality_;

  // Compressed image publishing settings
  bool publish_compressed_{true};
  int compressed_frame_interval_{3};  // Publish compressed every N frames
  int compressed_frame_counter_{0};

  // Stereo image publishing (combined left+right for legacy compatibility)
  bool publish_stereo_{false};

  // V4L2 control interface
  int camera_fd_{-1};
  
  // Camera control parameters
  int exposure_min_, exposure_max_;
  int gain_min_, gain_max_;
  int current_exposure_;
  int current_gain_;
  bool v4l2_controls_initialized_{false};
  int exposure_setting_{-1};
  int gain_setting_{-1};
  int default_gain_param_{110};

  // Auto exposure control
  AutoExposureController auto_exposure_controller_;
  AutoExposureMode auto_exposure_mode_{AutoExposureMode::HARDWARE};
  double target_brightness_{128.0};
  double ae_kp_{0.8};
  int auto_exposure_update_interval_{3};  // Update every N frames (10Hz for 30Hz camera)
  int frame_counter_{0};

  // OpenCV camera capture
  cv::VideoCapture cap_;

  // Frame timing tracking
  std::deque<rclcpp::Time> frame_timestamps_;
  rclcpp::Time last_stats_print_;
  
  // Statistics
  std::atomic<int> frame_count_{0};
  std::atomic<bool> camera_initialized_{false};

  // TurboJPEG encoder for faster JPEG compression
  std::unique_ptr<JpegTurboEncoder> jpeg_encoder_;
};

} // namespace maurice_cam

