// ROS message publishing: rectified images, disparity, depth.

#include "maurice_cam/stereo_depth_estimator.hpp"
#include <turbojpeg.h>

#include <algorithm>
#include <cmath>

namespace maurice_cam
{

// =============================================================================
// Publish mono-rectified left/right images (upscaled to input resolution)
// =============================================================================
void StereoDepthEstimator::publishMonoRectified(
    const cv::Mat& left_rect, const cv::Mat& right_rect,
    const rclcpp::Time& ts, bool pub_left, bool pub_right)
{
  cv::Mat left_full, right_full;
  cv::resize(left_rect,  left_full,  cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);
  cv::resize(right_rect, right_full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);

  auto make_msg = [&](const cv::Mat& img) {
    auto msg = std::make_unique<sensor_msgs::msg::Image>();
    msg->header.stamp = ts;
    msg->header.frame_id = frame_id_;
    msg->height = image_height_;
    msg->width  = image_width_;
    msg->encoding = "mono8";
    msg->is_bigendian = false;
    msg->step = image_width_;
    msg->data.resize(msg->height * msg->step);
    memcpy(msg->data.data(), img.data, msg->data.size());
    return msg;
  };

  if (pub_left)  left_rectified_pub_->publish(make_msg(left_full));
  if (pub_right) right_rectified_pub_->publish(make_msg(right_full));
}

// =============================================================================
// Publish colour-rectified image (BGR) and/or JPEG-compressed version
// =============================================================================
void StereoDepthEstimator::publishColorRectified(
    const cv::Mat& color_rect, const cv::Mat& mono_rect,
    bool has_color_input, const rclcpp::Time& ts,
    bool pub_color, bool pub_compressed)
{
  // Build colour source: use rectified colour if available, else convert mono
  cv::Mat src;
  if (has_color_input) {
    src = color_rect;
  } else {
    cv::cvtColor(mono_rect, src, cv::COLOR_GRAY2BGR);
  }

  cv::Mat full;
  cv::resize(src, full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);

  if (pub_color) {
    auto msg = std::make_unique<sensor_msgs::msg::Image>();
    msg->header.stamp = ts;
    msg->header.frame_id = frame_id_;
    msg->height = image_height_;
    msg->width  = image_width_;
    msg->encoding = "bgr8";
    msg->is_bigendian = false;
    msg->step = image_width_ * 3;
    msg->data.resize(msg->height * msg->step);
    memcpy(msg->data.data(), full.data, msg->data.size());
    left_rectified_color_pub_->publish(std::move(msg));
  }

  if (pub_compressed) {
    auto msg = std::make_unique<sensor_msgs::msg::CompressedImage>();
    msg->header.stamp = ts;
    msg->header.frame_id = frame_id_;
    msg->format = "jpeg";

    tjhandle tj = tjInitCompress();
    if (tj) {
      unsigned char* jpeg_buf = nullptr;
      unsigned long jpeg_size = 0;
      int rc = tjCompress2(tj, full.data,
                           full.cols, 0, full.rows,
                           TJPF_BGR, &jpeg_buf, &jpeg_size,
                           TJSAMP_420, jpeg_quality_, TJFLAG_FASTDCT);
      if (rc == 0 && jpeg_buf) {
        msg->data.assign(jpeg_buf, jpeg_buf + jpeg_size);
        left_rectified_compressed_pub_->publish(std::move(msg));
      }
      if (jpeg_buf) tjFree(jpeg_buf);
      tjDestroy(tj);
    }
  }
}

// =============================================================================
// Publish a DisparityImage message (upscaled to input resolution)
// =============================================================================
void StereoDepthEstimator::publishDisparityMsg(
    const cv::Mat& disparity_float, const rclcpp::Time& ts,
    rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr& pub)
{
  cv::Mat disp_full;
  cv::resize(disparity_float, disp_full, cv::Size(image_width_, image_height_),
             0, 0, cv::INTER_NEAREST);

  auto msg = std::make_unique<stereo_msgs::msg::DisparityImage>();
  msg->header.stamp = ts;
  msg->header.frame_id = frame_id_;
  msg->image.header = msg->header;
  msg->image.height = image_height_;
  msg->image.width  = image_width_;
  msg->image.encoding = "32FC1";
  msg->image.is_bigendian = false;
  msg->image.step = image_width_ * sizeof(float);
  msg->image.data.resize(msg->image.height * msg->image.step);
  memcpy(msg->image.data.data(), disp_full.data, msg->image.data.size());

  const float scale_up = 1.0f / static_cast<float>(depth_scale_);
  msg->f = static_cast<float>(focal_length_) * scale_up;
  msg->t = static_cast<float>(baseline_);
  msg->min_disparity = static_cast<float>(min_disparity_);
  msg->max_disparity = static_cast<float>(max_disparity_);
  msg->delta_d = 1.0f / 32.0f;  // VPI Q10.5

  pub->publish(std::move(msg));
}

// =============================================================================
// Convert disparity → 16SC1 depth (mm) and publish
// =============================================================================
void StereoDepthEstimator::publishDepth(
    const cv::Mat& disparity_float, const rclcpp::Time& ts)
{
  const float fb = static_cast<float>(focal_length_) *
                   std::abs(static_cast<float>(baseline_));
  const float MAX_DEPTH_M = 10.0f;

  cv::Mat depth_calib(calib_height_, calib_width_, CV_16SC1);
  for (int y = 0; y < calib_height_; y++) {
    const float* disp_row = disparity_float.ptr<float>(y);
    int16_t* depth_row = depth_calib.ptr<int16_t>(y);
    for (int x = 0; x < calib_width_; x++) {
      const float d = disp_row[x];
      if (d > 0.0f) {
        float z = fb / d;
        if (z > 0.0f && z <= MAX_DEPTH_M) {
          depth_row[x] = static_cast<int16_t>(
              std::clamp(z * 1000.0f, -32768.0f, 32767.0f));
        } else {
          depth_row[x] = 0;
        }
      } else {
        depth_row[x] = 0;
      }
    }
  }

  cv::Mat depth_full;
  cv::resize(depth_calib, depth_full, cv::Size(image_width_, image_height_),
             0, 0, cv::INTER_NEAREST);

  auto msg = std::make_unique<sensor_msgs::msg::Image>();
  msg->header.stamp = ts;
  msg->header.frame_id = frame_id_;
  msg->height = image_height_;
  msg->width  = image_width_;
  msg->encoding = "16SC1";
  msg->is_bigendian = false;
  msg->step = image_width_ * sizeof(int16_t);
  msg->data.resize(msg->height * msg->step);
  memcpy(msg->data.data(), depth_full.data, msg->data.size());
  depth_pub_->publish(std::move(msg));
}

// =============================================================================
// Compute the footprint convex-hull mask at calibration resolution.
// Stores the result in footprint_mask_calib_ (255 = robot body, 0 = free).
// Called once per frame — reused by publishing and the disparity filter chain.
// =============================================================================
void StereoDepthEstimator::computeFootprintMaskCalib()
{
  sensor_msgs::msg::PointCloud2::ConstSharedPtr cloud;
  {
    std::lock_guard<std::mutex> lock(footprint_mutex_);
    cloud = latest_footprint_cloud_;
  }

  footprint_mask_calib_ = cv::Mat::zeros(calib_height_, calib_width_, CV_8UC1);

  if (!cloud || cloud->width * cloud->height == 0) return;

  const float fx = static_cast<float>(P1_.at<double>(0, 0));
  const float fy = static_cast<float>(P1_.at<double>(1, 1));
  const float cx = static_cast<float>(P1_.at<double>(0, 2));
  const float cy = static_cast<float>(P1_.at<double>(1, 2));

  std::vector<cv::Point2f> projected_pts;

  sensor_msgs::PointCloud2ConstIterator<float> ix(*cloud, "x");
  sensor_msgs::PointCloud2ConstIterator<float> iy(*cloud, "y");
  sensor_msgs::PointCloud2ConstIterator<float> iz(*cloud, "z");

  for (; ix != ix.end(); ++ix, ++iy, ++iz) {
    const float X = *ix;
    const float Y = *iy;
    const float Z = *iz;
    if (Z <= 0.0f || !std::isfinite(X) || !std::isfinite(Y) || !std::isfinite(Z))
      continue;
    projected_pts.emplace_back(fx * X / Z + cx, fy * Y / Z + cy);
  }

  if (projected_pts.size() >= 3) {
    std::vector<cv::Point2f> hull;
    cv::convexHull(projected_pts, hull);
    std::vector<cv::Point> hull_int;
    hull_int.reserve(hull.size());
    for (const auto& p : hull)
      hull_int.emplace_back(static_cast<int>(p.x), static_cast<int>(p.y));
    cv::fillConvexPoly(footprint_mask_calib_, hull_int, cv::Scalar(255));
  }
}

// =============================================================================
// Project /footprint/camera_optical pointcloud onto the rectified colour image
// (points only — no convex hull)
// =============================================================================
void StereoDepthEstimator::publishFootprintOverlay(
    const cv::Mat& color_rect, const rclcpp::Time& ts)
{
  cv::Mat overlay = color_rect.clone();

  // Grab the latest footprint pointcloud for individual point drawing
  sensor_msgs::msg::PointCloud2::ConstSharedPtr cloud;
  {
    std::lock_guard<std::mutex> lock(footprint_mutex_);
    cloud = latest_footprint_cloud_;
  }

  if (cloud && cloud->width * cloud->height > 0) {
    const float fx = static_cast<float>(P1_.at<double>(0, 0));
    const float fy = static_cast<float>(P1_.at<double>(1, 1));
    const float cx = static_cast<float>(P1_.at<double>(0, 2));
    const float cy = static_cast<float>(P1_.at<double>(1, 2));

    sensor_msgs::PointCloud2ConstIterator<float> ix(*cloud, "x");
    sensor_msgs::PointCloud2ConstIterator<float> iy(*cloud, "y");
    sensor_msgs::PointCloud2ConstIterator<float> iz(*cloud, "z");

    for (; ix != ix.end(); ++ix, ++iy, ++iz) {
      const float X = *ix;
      const float Y = *iy;
      const float Z = *iz;
      if (Z <= 0.0f || !std::isfinite(X) || !std::isfinite(Y) || !std::isfinite(Z))
        continue;

      const int u = static_cast<int>(fx * X / Z + cx);
      const int v = static_cast<int>(fy * Y / Z + cy);
      cv::circle(overlay, cv::Point(u, v), 4, cv::Scalar(0, 255, 0), -1);
    }
  }

  // Upscale to input resolution and publish
  cv::Mat full;
  cv::resize(overlay, full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);

  auto msg = std::make_unique<sensor_msgs::msg::Image>();
  msg->header.stamp = ts;
  msg->header.frame_id = frame_id_;
  msg->height = image_height_;
  msg->width  = image_width_;
  msg->encoding = "bgr8";
  msg->is_bigendian = false;
  msg->step = image_width_ * 3;
  msg->data.resize(msg->height * msg->step);
  memcpy(msg->data.data(), full.data, msg->data.size());
  footprint_overlay_pub_->publish(std::move(msg));
}

// =============================================================================
// Binary mask of the footprint convex hull (mono8: 255 inside, 0 outside).
// Reuses footprint_mask_calib_ computed by computeFootprintMaskCalib().
// =============================================================================
void StereoDepthEstimator::publishFootprintMask(const rclcpp::Time& ts)
{
  cv::Mat full;
  cv::resize(footprint_mask_calib_, full,
             cv::Size(image_width_, image_height_), 0, 0, cv::INTER_NEAREST);

  auto msg = std::make_unique<sensor_msgs::msg::Image>();
  msg->header.stamp = ts;
  msg->header.frame_id = frame_id_;
  msg->height = image_height_;
  msg->width  = image_width_;
  msg->encoding = "mono8";
  msg->is_bigendian = false;
  msg->step = image_width_;
  msg->data.resize(msg->height * msg->step);
  memcpy(msg->data.data(), full.data, msg->data.size());
  footprint_mask_pub_->publish(std::move(msg));
}

// =============================================================================
// Rectified colour image with the footprint mask region blacked out.
// =============================================================================
void StereoDepthEstimator::publishFootprintCutout(
    const cv::Mat& color_rect, const rclcpp::Time& ts)
{
  cv::Mat cutout = color_rect.clone();
  cutout.setTo(cv::Scalar(0, 0, 0), footprint_mask_calib_);

  cv::Mat full;
  cv::resize(cutout, full, cv::Size(image_width_, image_height_), 0, 0, cv::INTER_LINEAR);

  auto msg = std::make_unique<sensor_msgs::msg::Image>();
  msg->header.stamp = ts;
  msg->header.frame_id = frame_id_;
  msg->height = image_height_;
  msg->width  = image_width_;
  msg->encoding = "bgr8";
  msg->is_bigendian = false;
  msg->step = image_width_ * 3;
  msg->data.resize(msg->height * msg->step);
  memcpy(msg->data.data(), full.data, msg->data.size());
  footprint_cutout_pub_->publish(std::move(msg));
}

// =============================================================================
// Zero out disparity pixels inside the footprint mask.
// Called as step 1 of the filter chain (always runs).
// =============================================================================
void StereoDepthEstimator::applyFootprintMask(cv::Mat& disparity)
{
  if (footprint_mask_calib_.empty()) return;

  // Mask may be at a different resolution than disparity (if downsample happened
  // before this call, which it shouldn't — this runs first).  Resize if needed.
  if (footprint_mask_calib_.size() != disparity.size()) {
    cv::Mat mask_resized;
    cv::resize(footprint_mask_calib_, mask_resized, disparity.size(),
               0, 0, cv::INTER_NEAREST);
    disparity.setTo(0.0f, mask_resized);
  } else {
    disparity.setTo(0.0f, footprint_mask_calib_);
  }
}

} // namespace maurice_cam
