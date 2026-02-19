// Camera-info based calibration: subscribe to left/right CameraInfo topics,
// extract K, D, R, P matrices, compute rectification maps, derive baseline
// and focal length, then kick off VPI initialisation.

#include "maurice_cam/stereo_depth_estimator.hpp"

namespace maurice_cam
{

// =============================================================================
// Camera info callbacks — collect left/right, (re-)init when data changes
// =============================================================================
static bool cameraInfoEqual(const sensor_msgs::msg::CameraInfo& a,
                            const sensor_msgs::msg::CameraInfo& b)
{
  return a.width == b.width && a.height == b.height
      && a.k == b.k && a.d == b.d && a.r == b.r && a.p == b.p;
}

void StereoDepthEstimator::leftCameraInfoCallback(
    const sensor_msgs::msg::CameraInfo::ConstSharedPtr& msg)
{
  if (left_camera_info_ && cameraInfoEqual(*left_camera_info_, *msg)) return;
  left_camera_info_ = msg;
  RCLCPP_INFO(this->get_logger(), "Received left camera_info (%dx%d)", msg->width, msg->height);
  if (right_camera_info_) initCalibrationFromCameraInfo();
}

void StereoDepthEstimator::rightCameraInfoCallback(
    const sensor_msgs::msg::CameraInfo::ConstSharedPtr& msg)
{
  if (right_camera_info_ && cameraInfoEqual(*right_camera_info_, *msg)) return;
  right_camera_info_ = msg;
  RCLCPP_INFO(this->get_logger(), "Received right camera_info (%dx%d)", msg->width, msg->height);
  if (left_camera_info_) initCalibrationFromCameraInfo();
}

bool StereoDepthEstimator::initCalibrationFromCameraInfo()
{
  std::lock_guard<std::mutex> lock(calib_mutex_);

  const auto& left  = *left_camera_info_;
  const auto& right = *right_camera_info_;

  // Check if cameras are calibrated (K[0] == 0.0 indicates uncalibrated)
  if (left.k[0] == 0.0 || right.k[0] == 0.0) {
    RCLCPP_WARN(this->get_logger(), 
                "Camera is uncalibrated (K[0] == 0.0). Depth estimation disabled.");
    calibration_loaded_ = false;
    return false;
  }

  calib_width_  = static_cast<int>(left.width);
  calib_height_ = static_cast<int>(left.height);
  depth_scale_  = static_cast<double>(calib_width_) / static_cast<double>(image_width_);

  // ── Extract matrices from CameraInfo messages ───────────────────────────
  // k/r/p are contiguous row-major doubles — same layout as cv::Mat(CV_64F).
  auto mat33  = [](const auto& a) { return cv::Mat(3, 3, CV_64F, const_cast<double*>(a.data())).clone(); };
  auto mat34  = [](const auto& a) { return cv::Mat(3, 4, CV_64F, const_cast<double*>(a.data())).clone(); };
  auto vecD   = [](const auto& v) { return cv::Mat(1, static_cast<int>(v.size()), CV_64F, const_cast<double*>(v.data())).clone(); };

  cv::Mat K1 = mat33(left.k),  R1 = mat33(left.r);
  P1_        = mat34(left.p);
  cv::Mat D1 = vecD(left.d);

  cv::Mat K2 = mat33(right.k), R2 = mat33(right.r), P2 = mat34(right.p);
  cv::Mat D2 = vecD(right.d);

  // ── Derive stereo parameters ───────────────────────────────────────────
  focal_length_ = P1_.at<double>(0, 0);

  // The camera driver negates P2[0,3] when building the ROS CameraInfo:
  //   OpenCV convention: P2[0,3] = +fx'·baseline
  //   ROS convention:    P2[0,3] = −fx'·baseline
  // We need |P2[0,3]| / fx' = baseline, and we must undo the sign flip
  // before passing P2 to initUndistortRectifyMap (which expects OpenCV convention).
  double tx_fx = P2.at<double>(0, 3);
  if (std::abs(tx_fx) > 1e-6) {
    baseline_ = std::abs(tx_fx) / P2.at<double>(0, 0);
  } else {
    RCLCPP_ERROR(this->get_logger(),
                 "Cannot determine baseline from camera_info (P2[0,3] ≈ 0)");
    return false;
  }

  // Restore OpenCV sign convention for P2[0,3] before computing remap tables.
  // OpenCV expects P2[0,3] = +fx'·baseline (positive).
  cv::Mat P2_cv = P2.clone();
  P2_cv.at<double>(0, 3) = std::abs(P2_cv.at<double>(0, 3));

  // ── Compute rectification maps ─────────────────────────────────────────
  cv::initUndistortRectifyMap(K1, D1, R1, P1_,
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_left_, map2_left_);
  cv::initUndistortRectifyMap(K2, D2, R2, P2_cv,
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_right_, map2_right_);

  RCLCPP_INFO(this->get_logger(), "Calibration extracted from camera_info:");
  RCLCPP_INFO(this->get_logger(), "  Resolution: %dx%d", calib_width_, calib_height_);
  RCLCPP_INFO(this->get_logger(), "  Scale: input %dx%d -> calib %dx%d (scale=%.3f)",
              image_width_, image_height_, calib_width_, calib_height_, depth_scale_);
  RCLCPP_INFO(this->get_logger(), "  Stereo: focal=%.2f px, baseline=%.4f m (%.1f mm)",
              focal_length_, baseline_, baseline_ * 1000.0);

  // ── (Re-)Initialize VPI (remap + SGM) ──────────────────────────────────
  // Tear down existing resources if this is a recalibration.
  if (vpi_initialized_) {
    calibration_loaded_ = false;  // gate processFrame while we reinit
    vpi_initialized_    = false;
    cleanupVPIRemap();
    cleanupVPI();
    RCLCPP_INFO(this->get_logger(), "Recalibration: cleaned up previous VPI resources");
  }

  if (!initVPIRemap()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to initialize VPI remap from camera_info");
    return false;
  }
  if (!initializeVPI()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to initialize VPI from camera_info");
    return false;
  }
  vpi_initialized_ = true;
  calibration_loaded_ = true;
  RCLCPP_INFO(this->get_logger(), "VPI initialized (SGM CUDA, diagonals=%d)", include_diagonals_);
  return true;
}

} // namespace maurice_cam
