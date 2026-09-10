// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Point cloud generation from disparity maps (xyz-only and xyzrgb).

#include "mars_cam/stereo_depth_estimator.hpp"

#include <cmath>
#include <cstring>
#include <limits>
#include <vector>

#include <tf2/LinearMath/Matrix3x3.h>
#include <tf2/LinearMath/Quaternion.h>
#include <geometry_msgs/msg/transform_stamped.hpp>

namespace mars_cam {

// =============================================================================
// XYZ-only point cloud (no colour overhead)
// =============================================================================
void StereoDepthEstimator::publishPointCloudXYZ(const cv::Mat& disparity_lowres, const rclcpp::Time& ts) {
    const int dw = disparity_lowres.cols;
    const int dh = disparity_lowres.rows;
    const float MAX_DEPTH_M = 10.0f;

    // Scale pixel-coordinate intrinsics to the downsampled grid.
    // Depth focal length stays at calibration-res (disparity values are in those units).
    const float s = static_cast<float>(dw) / static_cast<float>(calib_width_);
    const float fx = static_cast<float>(P1_.at<double>(0, 0)) * s;
    const float fy = static_cast<float>(P1_.at<double>(1, 1)) * s;
    const float cx = static_cast<float>(P1_.at<double>(0, 2)) * s;
    const float cy = static_cast<float>(P1_.at<double>(1, 2)) * s;
    const float f_depth = static_cast<float>(focal_length_);
    const float baseline = static_cast<float>(baseline_);
    // Back-projection lands in the rectified frame; this rotates into the frame
    // the cloud is actually stamped with. See updateCloudRotation().
    const cv::Matx33f& R = cloud_rotation_;

    const int step = pointcloud_decimation_;
    const int pc_w = dw / step;
    const int pc_h = dh / step;

    auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
    cloud->header.stamp = ts;
    cloud->header.frame_id = frame_id_;
    cloud->height = pc_h;
    cloud->width = pc_w;
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
                    const cv::Vec3f p = R * cv::Vec3f((static_cast<float>(px) - cx) * z / fx,
                                                      (static_cast<float>(py) - cy) * z / fy, z);
                    *ix = p[0];
                    *iy = p[1];
                    *iz = p[2];
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
void StereoDepthEstimator::publishPointCloudColor(const cv::Mat& disparity_lowres, const cv::Mat& color_rect,
                                                  const rclcpp::Time& ts) {
    const int dw = disparity_lowres.cols;
    const int dh = disparity_lowres.rows;
    const float MAX_DEPTH_M = 10.0f;

    const float s = static_cast<float>(dw) / static_cast<float>(calib_width_);
    const float fx = static_cast<float>(P1_.at<double>(0, 0)) * s;
    const float fy = static_cast<float>(P1_.at<double>(1, 1)) * s;
    const float cx = static_cast<float>(P1_.at<double>(0, 2)) * s;
    const float cy = static_cast<float>(P1_.at<double>(1, 2)) * s;
    const float f_depth = static_cast<float>(focal_length_);
    const float baseline = static_cast<float>(baseline_);
    // Back-projection lands in the rectified frame; this rotates into the frame
    // the cloud is actually stamped with. See updateCloudRotation().
    const cv::Matx33f& R = cloud_rotation_;

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
    cloud->header.stamp = ts;
    cloud->header.frame_id = frame_id_;
    cloud->height = pc_h;
    cloud->width = pc_w;
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
                    const cv::Vec3f p = R * cv::Vec3f((static_cast<float>(px) - cx) * z / fx,
                                                      (static_cast<float>(py) - cy) * z / fy, z);
                    *ix = p[0];
                    *iy = p[1];
                    *iz = p[2];

                    if (has_color) {
                        const cv::Vec3b& bgr = color_ds.at<cv::Vec3b>(py, px);
                        uint32_t rgb_packed = (static_cast<uint32_t>(bgr[2]) << 16) |
                                              (static_cast<uint32_t>(bgr[1]) << 8) | (static_cast<uint32_t>(bgr[0]));
                        float rgb_float;
                        std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
                        *irgb = rgb_float;
                    } else {
                        uint32_t rgb_packed = 0x00808080;
                        float rgb_float;
                        std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
                        *irgb = rgb_float;
                    }
                    continue;
                }
            }
            *ix = std::numeric_limits<float>::quiet_NaN();
            *iy = std::numeric_limits<float>::quiet_NaN();
            *iz = std::numeric_limits<float>::quiet_NaN();
            uint32_t rgb_packed = 0x00000000;
            float rgb_float;
            std::memcpy(&rgb_float, &rgb_packed, sizeof(float));
            *irgb = rgb_float;
        }
    }

    pointcloud_color_pub_->publish(std::move(cloud));
}

// =============================================================================
// Forward traversability corridor — the only cloud the costmap consumes
// =============================================================================
// Published in base_link, already height-filtered, so the costmap layer needs
// no height threshold of its own. Deliberately narrow: pitch error scales with
// range, the image periphery is where this 98-degree lens fits worst, and every
// point outside the corridor is a chance to mark something the robot would
// never have driven into.
void StereoDepthEstimator::publishPointCloudNav(const cv::Mat& disparity_lowres, const rclcpp::Time& ts) {
    geometry_msgs::msg::TransformStamped tf;
    try {
        // TimePointZero: the head moves slowly relative to an 8Hz frame rate,
        // and a lookup at the exact frame stamp fails whenever TF lags.
        tf = tf_buffer_->lookupTransform(nav_frame_, frame_id_, tf2::TimePointZero);
    } catch (const tf2::TransformException& e) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "No transform %s <- %s: %s",
                             nav_frame_.c_str(), frame_id_.c_str(), e.what());
        return;
    }

    const auto& q = tf.transform.rotation;
    const auto& t = tf.transform.translation;
    const tf2::Matrix3x3 basis(tf2::Quaternion(q.x, q.y, q.z, q.w));
    cv::Matx33f optical_to_nav;
    for (int r = 0; r < 3; ++r)
        for (int c = 0; c < 3; ++c)
            optical_to_nav(r, c) = static_cast<float>(basis[r][c]);
    // The height correction rides on the camera origin: TF places the camera
    // from nominal CAD, and the real mount differs by a measurable offset that
    // otherwise lifts the whole floor toward the marking threshold.
    const cv::Vec3f origin(static_cast<float>(t.x), static_cast<float>(t.y),
                           static_cast<float>(t.z + mount_height_correction_m_));

    // One matrix from rectified pixels straight to base_link.
    const cv::Matx33f to_nav = optical_to_nav * cloud_rotation_;

    const int dw = disparity_lowres.cols;
    const int dh = disparity_lowres.rows;
    const float s = static_cast<float>(dw) / static_cast<float>(calib_width_);
    const float fx = static_cast<float>(P1_.at<double>(0, 0)) * s;
    const float fy = static_cast<float>(P1_.at<double>(1, 1)) * s;
    const float cx = static_cast<float>(P1_.at<double>(0, 2)) * s;
    const float cy = static_cast<float>(P1_.at<double>(1, 2)) * s;
    const float f_depth = static_cast<float>(focal_length_);
    const float baseline = static_cast<float>(baseline_);
    const int step = pointcloud_decimation_;

    std::vector<cv::Vec3f> kept;
    kept.reserve(static_cast<size_t>((dw / step) * (dh / step)) / 4);

    for (int py = 0; py < dh; py += step) {
        for (int px = 0; px < dw; px += step) {
            const float d = disparity_lowres.at<float>(py, px);
            if (d <= 0.0f || !std::isfinite(d))
                continue;
            const float z = f_depth * baseline / d;
            if (z <= 0.0f || z > nav_roi_x_max_ * 2.0f)
                continue;
            const cv::Vec3f p =
                to_nav * cv::Vec3f((static_cast<float>(px) - cx) * z / fx, (static_cast<float>(py) - cy) * z / fy, z) +
                origin;
            if (p[0] < nav_roi_x_min_ || p[0] > nav_roi_x_max_)
                continue;
            if (std::abs(p[1]) > nav_roi_half_width_)
                continue;
            if (p[2] < nav_roi_z_min_ || p[2] > nav_roi_z_max_)
                continue;
            kept.push_back(p);
        }
    }

    auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
    cloud->header.stamp = ts;
    cloud->header.frame_id = nav_frame_;
    cloud->height = 1;
    cloud->width = static_cast<uint32_t>(kept.size());
    cloud->is_dense = true;
    cloud->is_bigendian = false;

    sensor_msgs::PointCloud2Modifier mod(*cloud);
    mod.setPointCloud2FieldsByString(1, "xyz");
    mod.resize(kept.size());

    sensor_msgs::PointCloud2Iterator<float> ix(*cloud, "x");
    sensor_msgs::PointCloud2Iterator<float> iy(*cloud, "y");
    sensor_msgs::PointCloud2Iterator<float> iz(*cloud, "z");
    for (const auto& p : kept) {
        *ix = p[0];
        *iy = p[1];
        *iz = p[2];
        ++ix;
        ++iy;
        ++iz;
    }

    pointcloud_nav_pub_->publish(std::move(cloud));
}

}  // namespace mars_cam
