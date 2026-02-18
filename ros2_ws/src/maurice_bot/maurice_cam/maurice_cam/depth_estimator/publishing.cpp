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

} // namespace maurice_cam
