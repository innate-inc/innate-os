// Simple / OpenCV-delegating disparity filters:
//   median, bilateral, hole-fill, depth-clamp, edge-invalidation, speckle.

#include "maurice_cam/stereo_depth_estimator.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace maurice_cam
{

// =============================================================================
// Median
// =============================================================================
void StereoDepthEstimator::applyMedian(cv::Mat& img)
{
  if (median_kernel_size_ <= 5) {
    cv::medianBlur(img, img, median_kernel_size_);
  } else {
    double mn, mx;
    cv::minMaxLoc(img, &mn, &mx);
    if (mx <= 0) return;
    cv::Mat u8;
    img.convertTo(u8, CV_8U, 255.0 / mx);
    cv::medianBlur(u8, u8, median_kernel_size_);
    u8.convertTo(img, CV_32F, mx / 255.0);
  }
}

// =============================================================================
// Bilateral
// =============================================================================
void StereoDepthEstimator::applyBilateral(cv::Mat& img)
{
  cv::Mat src = img.clone();
  cv::bilateralFilter(src, img, bilateral_diameter_,
                      bilateral_sigma_color_, bilateral_sigma_space_);
}

// =============================================================================
// Hole filling (4-neighbour search, configurable strategy)
// =============================================================================
void StereoDepthEstimator::fillHoles(cv::Mat& img)
{
  const int rows = img.rows, cols = img.cols;
  const int max_r = (hole_fill_radius_ >= 9999) ? std::max(rows, cols) : hole_fill_radius_;

  // Strategy 0: fill from left neighbour only (RealSense mode 0)
  // Strategy 1: farthest from around — pick neighbour with smallest disparity
  //             (= farthest depth). Safest, avoids foreground bleed. (RS default)
  // Strategy 2: nearest from around — pick neighbour with largest disparity
  //             (= nearest depth).
  const int strategy = hole_fill_strategy_;

  // Work on a clone so reads don't see writes from this pass
  cv::Mat src = img.clone();

  for (int y = 0; y < rows; ++y) {
    float* dst_row = img.ptr<float>(y);
    for (int x = 0; x < cols; ++x) {
      if (src.at<float>(y, x) > 0) continue;

      // Gather up to 4 neighbours (left, right, up, down)
      float candidates[4] = {0, 0, 0, 0};
      int count = 0;

      // Left
      for (int d = 1; d <= max_r && x - d >= 0; ++d)
        if (src.at<float>(y, x - d) > 0) { candidates[count++] = src.at<float>(y, x - d); break; }
      // Right
      for (int d = 1; d <= max_r && x + d < cols; ++d)
        if (src.at<float>(y, x + d) > 0) { candidates[count++] = src.at<float>(y, x + d); break; }
      // Up
      for (int d = 1; d <= max_r && y - d >= 0; ++d)
        if (src.at<float>(y - d, x) > 0) { candidates[count++] = src.at<float>(y - d, x); break; }
      // Down
      for (int d = 1; d <= max_r && y + d < rows; ++d)
        if (src.at<float>(y + d, x) > 0) { candidates[count++] = src.at<float>(y + d, x); break; }

      if (count == 0) continue;

      if (strategy == 0) {
        dst_row[x] = candidates[0];
      } else if (strategy == 1) {
        float best = candidates[0];
        for (int i = 1; i < count; ++i)
          if (candidates[i] < best) best = candidates[i];
        dst_row[x] = best;
      } else {
        float best = candidates[0];
        for (int i = 1; i < count; ++i)
          if (candidates[i] > best) best = candidates[i];
        dst_row[x] = best;
      }
    }
  }
}

// =============================================================================
// Depth clamping (disparity ↔ depth conversion)
// =============================================================================
void StereoDepthEstimator::clampByDepth(cv::Mat& img, float f, float t)
{
  const float abs_t = std::abs(t);
  if (f <= 0 || abs_t <= 0) return;
  float max_d = (min_depth_meters_ > 0)
    ? f * abs_t / static_cast<float>(min_depth_meters_)
    : std::numeric_limits<float>::max();
  float min_d = (max_depth_meters_ > 0)
    ? f * abs_t / static_cast<float>(max_depth_meters_)
    : 0.0f;
  int killed_near = 0, killed_far = 0, total_valid = 0;
  float d_min_seen = std::numeric_limits<float>::max(), d_max_seen = 0.0f;
  for (int y = 0; y < img.rows; ++y) {
    float* row = img.ptr<float>(y);
    for (int x = 0; x < img.cols; ++x) {
      float d = row[x];
      if (d <= 0) continue;
      total_valid++;
      if (d < d_min_seen) d_min_seen = d;
      if (d > d_max_seen) d_max_seen = d;
      if (d > max_d) { row[x] = 0.0f; killed_near++; }
      else if (d < min_d) { row[x] = 0.0f; killed_far++; }
    }
  }
  RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
    "depth_clamp: f=%.2f t=%.5f | min_depth=%.3fm -> max_d=%.1f | max_depth=%.1fm -> min_d=%.1f | "
    "disp_range=[%.2f, %.2f] | killed: %d near + %d far / %d valid",
    f, abs_t, min_depth_meters_, max_d, max_depth_meters_, min_d,
    d_min_seen, d_max_seen, killed_near, killed_far, total_valid);
}

// =============================================================================
// Edge invalidation (Canny + dilation → zero out edge-adjacent disparities)
// =============================================================================
void StereoDepthEstimator::invalidateEdges(cv::Mat& img)
{
  double mn, mx;
  cv::minMaxLoc(img, &mn, &mx);
  if (mx <= 0) return;
  cv::Mat u8;
  img.convertTo(u8, CV_8U, 255.0 / mx);
  cv::GaussianBlur(u8, u8, cv::Size(3, 3), 0.8);
  cv::Mat edges;
  cv::Canny(u8, edges, edge_canny_low_, edge_canny_high_);
  if (edge_inv_width_ > 1) {
    int k = edge_inv_width_ | 1;
    cv::Mat kernel = cv::getStructuringElement(cv::MORPH_ELLIPSE, cv::Size(k, k));
    cv::dilate(edges, edges, kernel);
  }
  for (int y = 0; y < img.rows; ++y) {
    float* dr = img.ptr<float>(y);
    const uchar* er = edges.ptr<uchar>(y);
    for (int x = 0; x < img.cols; ++x)
      if (er[x] > 0 && dr[x] > 0) dr[x] = 0.0f;
  }
}

// =============================================================================
// Speckle removal (via OpenCV)
// =============================================================================
void StereoDepthEstimator::filterSpeckles(cv::Mat& img)
{
  const float scale = 16.0f;
  cv::Mat s16;
  img.convertTo(s16, CV_16SC1, scale);
  cv::filterSpeckles(s16, 0, speckle_max_size_, speckle_max_diff_ * scale);
  s16.convertTo(img, CV_32FC1, 1.0 / scale);
}

} // namespace maurice_cam
