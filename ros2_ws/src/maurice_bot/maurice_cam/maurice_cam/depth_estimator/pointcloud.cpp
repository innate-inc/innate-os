// Point cloud generation from disparity maps (xyz-only and xyzrgb).

#include "maurice_cam/stereo_depth_estimator.hpp"

#include <cmath>
#include <cstring>
#include <limits>

namespace maurice_cam
{

// =============================================================================
// XYZ-only point cloud (no colour overhead)
// =============================================================================
void StereoDepthEstimator::publishPointCloudXYZ(
    const cv::Mat& disparity_lowres, const rclcpp::Time& ts)
{
  const int dw = disparity_lowres.cols;
  const int dh = disparity_lowres.rows;
  const float MAX_DEPTH_M = 10.0f;

  // Scale pixel-coordinate intrinsics to the downsampled grid.
  // Depth focal length stays at calibration-res (disparity values are in those units).
  const float s  = static_cast<float>(dw) / static_cast<float>(calib_width_);
  const float fx = static_cast<float>(stereo_calib_->P1().at<double>(0, 0)) * s;
  const float fy = static_cast<float>(stereo_calib_->P1().at<double>(1, 1)) * s;
  const float cx = static_cast<float>(stereo_calib_->P1().at<double>(0, 2)) * s;
  const float cy = static_cast<float>(stereo_calib_->P1().at<double>(1, 2)) * s;
  const float f_depth  = static_cast<float>(focal_length_);
  const float baseline = static_cast<float>(baseline_);

  const int step = pointcloud_decimation_;
  const int pc_w = dw / step;
  const int pc_h = dh / step;

  auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
  cloud->header.stamp    = ts;
  cloud->header.frame_id = frame_id_;
  cloud->height = pc_h;
  cloud->width  = pc_w;
  cloud->is_dense = false;
  cloud->is_bigendian = false;

  sensor_msgs::PointCloud2Modifier mod(*cloud);
  mod.setPointCloud2FieldsByString(1, "xyz");
  mod.resize(pc_w * pc_h);

  sensor_msgs::PointCloud2Iterator<float> ix(*cloud, "x");
  sensor_msgs::PointCloud2Iterator<float> iy(*cloud, "y");
  sensor_msgs::PointCloud2Iterator<float> iz(*cloud, "z");

  for (int v = 0; v < pc_h; ++v) {
    for (int u = 0; u < pc_w; ++u, ++ix, ++iy, ++iz) {
      const int px = u * step;
      const int py = v * step;
      const float d = disparity_lowres.at<float>(py, px);

      if (d > 0.0f && std::isfinite(d)) {
        float z = f_depth * baseline / d;
        if (z > 0.0f && z <= MAX_DEPTH_M) {
          *ix = (static_cast<float>(px) - cx) * z / fx;
          *iy = (static_cast<float>(py) - cy) * z / fy;
          *iz = z;
          continue;
        }
      }
      *ix = std::numeric_limits<float>::quiet_NaN();
      *iy = std::numeric_limits<float>::quiet_NaN();
      *iz = std::numeric_limits<float>::quiet_NaN();
    }
  }

  pointcloud_pub_->publish(std::move(cloud));
}

// =============================================================================
// Colour point cloud (xyz + rgb)
// =============================================================================
void StereoDepthEstimator::publishPointCloudColor(
    const cv::Mat& disparity_lowres, const cv::Mat& color_rect,
    const rclcpp::Time& ts)
{
  const int dw = disparity_lowres.cols;
  const int dh = disparity_lowres.rows;
  const float MAX_DEPTH_M = 10.0f;

  const float s  = static_cast<float>(dw) / static_cast<float>(calib_width_);
  const float fx = static_cast<float>(stereo_calib_->P1().at<double>(0, 0)) * s;
  const float fy = static_cast<float>(stereo_calib_->P1().at<double>(1, 1)) * s;
  const float cx = static_cast<float>(stereo_calib_->P1().at<double>(0, 2)) * s;
  const float cy = static_cast<float>(stereo_calib_->P1().at<double>(1, 2)) * s;
  const float f_depth  = static_cast<float>(focal_length_);
  const float baseline = static_cast<float>(baseline_);

  // Downsample rectified colour image to match disparity resolution
  cv::Mat color_ds;
  const bool has_color = !color_rect.empty();
  if (has_color) {
    cv::resize(color_rect, color_ds, cv::Size(dw, dh), 0, 0, cv::INTER_AREA);
  }

  const int step = pointcloud_decimation_;
  const int pc_w = dw / step;
  const int pc_h = dh / step;

  auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
  cloud->header.stamp    = ts;
  cloud->header.frame_id = frame_id_;
  cloud->height = pc_h;
  cloud->width  = pc_w;
  cloud->is_dense = false;
  cloud->is_bigendian = false;

  sensor_msgs::PointCloud2Modifier mod(*cloud);
  mod.setPointCloud2FieldsByString(2, "xyz", "rgb");
  mod.resize(pc_w * pc_h);

  sensor_msgs::PointCloud2Iterator<float> ix(*cloud, "x");
  sensor_msgs::PointCloud2Iterator<float> iy(*cloud, "y");
  sensor_msgs::PointCloud2Iterator<float> iz(*cloud, "z");
  sensor_msgs::PointCloud2Iterator<float> irgb(*cloud, "rgb");

  for (int v = 0; v < pc_h; ++v) {
    for (int u = 0; u < pc_w; ++u, ++ix, ++iy, ++iz, ++irgb) {
      const int px = u * step;
      const int py = v * step;
      const float d = disparity_lowres.at<float>(py, px);

      if (d > 0.0f && std::isfinite(d)) {
        float z = f_depth * baseline / d;
        if (z > 0.0f && z <= MAX_DEPTH_M) {
          *ix = (static_cast<float>(px) - cx) * z / fx;
          *iy = (static_cast<float>(py) - cy) * z / fy;
          *iz = z;

          if (has_color) {
            const cv::Vec3b& bgr = color_ds.at<cv::Vec3b>(py, px);
            uint32_t rgb_packed = (static_cast<uint32_t>(bgr[2]) << 16) |
                                  (static_cast<uint32_t>(bgr[1]) << 8)  |
                                  (static_cast<uint32_t>(bgr[0]));
            float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
            *irgb = rgb_float;
          } else {
            uint32_t rgb_packed = 0x00808080;
            float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
            *irgb = rgb_float;
          }
          continue;
        }
      }
      *ix = std::numeric_limits<float>::quiet_NaN();
      *iy = std::numeric_limits<float>::quiet_NaN();
      *iz = std::numeric_limits<float>::quiet_NaN();
      uint32_t rgb_packed = 0x00000000;
      float rgb_float; std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
      *irgb = rgb_float;
    }
  }

  pointcloud_color_pub_->publish(std::move(cloud));
}

} // namespace maurice_cam
