// Image preprocessing: scale to calibration resolution, mono/color rectification.

#include "maurice_cam/stereo_depth_estimator.hpp"

namespace maurice_cam
{

// =============================================================================
// Resize both images to calibration resolution
// =============================================================================
void StereoDepthEstimator::scaleToCalibRes(
    const cv::Mat& left_in, const cv::Mat& right_in,
    cv::Mat& left_out, cv::Mat& right_out)
{
  cv::resize(left_in,  left_out,  cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_in, right_out, cv::Size(calib_width_, calib_height_), 0, 0, cv::INTER_LINEAR);
}

// =============================================================================
// Convert to grayscale (if colour) then apply stereo rectification maps
// =============================================================================
void StereoDepthEstimator::rectifyMono(
    const cv::Mat& left_scaled, const cv::Mat& right_scaled,
    cv::Mat& left_rect, cv::Mat& right_rect)
{
  cv::Mat left_gray, right_gray;
  if (left_scaled.channels() == 3) {
    cv::cvtColor(left_scaled,  left_gray,  cv::COLOR_BGR2GRAY);
    cv::cvtColor(right_scaled, right_gray, cv::COLOR_BGR2GRAY);
  } else {
    left_gray  = left_scaled;
    right_gray = right_scaled;
  }

  cv::remap(left_gray,  left_rect,  map1_left_,  map2_left_,  cv::INTER_LINEAR);
  cv::remap(right_gray, right_rect, map1_right_, map2_right_, cv::INTER_LINEAR);
}

// =============================================================================
// Rectify the colour image (needed for colour point cloud / colour rect topic)
// =============================================================================
void StereoDepthEstimator::rectifyColor(
    const cv::Mat& left_scaled, cv::Mat& left_color_rect)
{
  cv::remap(left_scaled, left_color_rect, map1_left_, map2_left_, cv::INTER_LINEAR);
}

} // namespace maurice_cam
