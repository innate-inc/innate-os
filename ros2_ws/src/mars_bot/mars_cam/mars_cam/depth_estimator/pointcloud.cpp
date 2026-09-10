// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Point cloud generation from disparity maps (xyz-only and xyzrgb).

#include "mars_cam/stereo_depth_estimator.hpp"

#include <cmath>
#include <cstring>
#include <limits>
#include <algorithm>
#include <array>
#include <iomanip>
#include <sstream>
#include <vector>

#include <diagnostic_msgs/msg/diagnostic_status.hpp>
#include <diagnostic_msgs/msg/key_value.hpp>
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
void StereoDepthEstimator::publishPointCloudNav(const cv::Mat& disparity_lowres, const cv::Mat& confidence,
                                                const rclcpp::Time& ts) {
    geometry_msgs::msg::TransformStamped tf_base, tf_odom;
    try {
        // TimePointZero: the head moves slowly relative to an 8Hz frame rate,
        // and a lookup at the exact frame stamp fails whenever TF lags.
        tf_base = tf_buffer_->lookupTransform(nav_frame_, frame_id_, tf2::TimePointZero);
        // Evidence must accumulate in a frame that does not move with the robot,
        // or driving forward would smear every voxel it has learned.
        tf_odom = tf_buffer_->lookupTransform(evidence_frame_, nav_frame_, tf2::TimePointZero);
    } catch (const tf2::TransformException& e) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "Nav cloud transform unavailable: %s",
                             e.what());
        return;
    }

    const auto to_matrix = [](const geometry_msgs::msg::TransformStamped& tf, cv::Matx33f& R, cv::Vec3f& t) {
        const auto& q = tf.transform.rotation;
        const tf2::Matrix3x3 basis(tf2::Quaternion(q.x, q.y, q.z, q.w));
        for (int r = 0; r < 3; ++r)
            for (int c = 0; c < 3; ++c)
                R(r, c) = static_cast<float>(basis[r][c]);
        t = cv::Vec3f(static_cast<float>(tf.transform.translation.x), static_cast<float>(tf.transform.translation.y),
                      static_cast<float>(tf.transform.translation.z));
    };

    cv::Matx33f R_base, R_odom;
    cv::Vec3f t_base, t_odom;
    to_matrix(tf_base, R_base, t_base);
    to_matrix(tf_odom, R_odom, t_odom);
    // The height correction rides on the camera origin: TF places the camera
    // from nominal CAD, and the real mount differs by a measurable offset that
    // otherwise lifts the whole floor toward the marking threshold.
    t_base[2] += static_cast<float>(mount_height_correction_m_);

    // One matrix from rectified pixels straight to base_link.
    const cv::Matx33f to_nav = R_base * cloud_rotation_;

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
    // Confidence is at calibration resolution; disparity may be downsampled.
    const float conf_scale = confidence.empty() ? 0.0f : static_cast<float>(confidence.cols) / static_cast<float>(dw);

    // Pass one: everything in the footprint column, at any height, so the floor
    // fit sees the surface rather than only what already cleared the threshold.
    // Slightly wider than the corridor — more floor constrains the plane better,
    // and none of it is marked.
    struct ColumnPoint {
        cv::Vec3f base;
        float range;
        int px;
        int py;
    };
    std::vector<std::array<double, 3>> ground_candidates;
    std::vector<ColumnPoint> in_column;
    ground_candidates.reserve(static_cast<size_t>((dw / step) * (dh / step)) / 4);
    in_column.reserve(ground_candidates.capacity());
    size_t weighted_points = 0;
    double match_confidence_sum = 0.0;
    double range_confidence_sum = 0.0;
    double weight_sum = 0.0;
    float match_confidence_min = 1.0f;
    float match_confidence_max = 0.0f;

    for (int py = 0; py < dh; py += step) {
        for (int px = 0; px < dw; px += step) {
            const float d = disparity_lowres.at<float>(py, px);
            if (d <= 0.0f || !std::isfinite(d))
                continue;
            const float z = f_depth * baseline / d;
            if (z <= 0.0f || z > nav_roi_x_max_ * 2.0f)
                continue;
            const cv::Vec3f in_base =
                to_nav * cv::Vec3f((static_cast<float>(px) - cx) * z / fx, (static_cast<float>(py) - cy) * z / fy, z) +
                t_base;
            if (in_base[0] < nav_roi_x_min_ || in_base[0] > nav_roi_x_max_)
                continue;
            if (std::abs(in_base[1]) > nav_roi_half_width_ + ground_search_extra_width_m_)
                continue;
            ground_candidates.push_back({in_base[0], in_base[1], in_base[2]});
            if (std::abs(in_base[1]) > nav_roi_half_width_)
                continue;
            in_column.push_back({in_base, z, px, py});
        }
    }

    // Fit the floor, then measure every candidate against IT rather than
    // against base_link z. A wrong camera mount, a head at an unexpected angle,
    // and a robot pitching over a floor transition all stop mattering — the
    // last of those otherwise makes a flat floor read 90mm high for ~1s and
    // marks the whole corridor.
    if (ground_estimation_enabled_) {
        const auto fit = ground_.update(ground_candidates);
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                             "ground: %s pitch %+.2f roll %+.2f offset %+.1fmm (%zu/%zu inliers, rms %.1fmm)%s",
                             fit.accepted ? "fit" : fit.rejection, ground_.plane().pitch_deg(),
                             ground_.plane().roll_deg(), ground_.plane().offset_m * 1000.0, fit.inliers,
                             fit.candidates, fit.residual_rms_m * 1000.0,
                             ground_.plane().valid ? "" : " [FALLBACK: base_link z]");
    }
    const GroundPlane& ground = ground_.plane();

    std::vector<Observation> observations;
    observations.reserve(in_column.size());

    {
        for (const auto& cp : in_column) {
            const cv::Vec3f& in_base = cp.base;
            const float z = cp.range;
            // With no valid fit this is exactly in_base[2], i.e. the previous
            // behaviour, so a blind or cluttered start degrades rather than fails.
            const double height = ground_estimation_enabled_
                                      ? ground.height_above(in_base[0], in_base[1], in_base[2])
                                      : in_base[2];
            if (height < nav_roi_z_min_ || height > nav_roi_z_max_)
                continue;

            float match_confidence = 1.0f;
            if (conf_scale > 0.0f) {
                const int cxi = std::min(static_cast<int>(cp.px * conf_scale), confidence.cols - 1);
                const int cyi = std::min(static_cast<int>(cp.py * conf_scale), confidence.rows - 1);
                match_confidence = confidence.at<float>(cyi, cxi);
            }
            const float range_weight =
                range_confidence(z, static_cast<float>(confidence_full_trust_m_), static_cast<float>(confidence_no_trust_m_));
            const float weight = match_confidence * range_weight;
            if (weight <= 0.0f)
                continue;

            ++weighted_points;
            match_confidence_sum += match_confidence;
            range_confidence_sum += range_weight;
            weight_sum += weight;
            match_confidence_min = std::min(match_confidence_min, match_confidence);
            match_confidence_max = std::max(match_confidence_max, match_confidence);

            const cv::Vec3f in_odom = R_odom * in_base + t_odom;
            observations.push_back({in_odom[0], in_odom[1], in_odom[2], weight, z});
        }
    }

    std::vector<std::array<float, 3>> marks;
    EvidenceGrid::IntegrateStats evidence_stats;
    double dt = 0.0;
    if (evidence_enabled_) {
        dt = last_evidence_stamp_.nanoseconds() == 0 ? 0.0 : std::clamp((ts - last_evidence_stamp_).seconds(), 0.0, 1.0);
        last_evidence_stamp_ = ts;
        evidence_stats = evidence_.integrate(observations, dt);
        marks = evidence_.confirmed();
        // Five stages that all fail by silently dropping points, so without a
        // line in the log the only symptom is an empty topic and no clue which
        // stage ate them. The stats topic carries the same funnel in machine
        // form, but only while something subscribes — this is what the operator
        // standing next to the robot sees in `innate view`.
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                             "nav cloud: %zu in corridor -> %zu voxels -> %zu supported -> %zu confirmed "
                             "(mean weight %.3f, %zu tracked)",
                             evidence_stats.observations, evidence_stats.voxels_seen, evidence_stats.voxels_supported,
                             evidence_stats.confirmed, evidence_stats.mean_weight, evidence_stats.tracked);
    } else {
        marks.reserve(observations.size());
        for (const auto& o : observations)
            marks.push_back({o.x, o.y, o.z});
        RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                             "nav cloud: %zu in corridor -> %zu published (evidence filter OFF)", observations.size(),
                             marks.size());
    }

    if (pointcloud_nav_stats_pub_->get_subscription_count() > 0) {
        diagnostic_msgs::msg::DiagnosticStatus status;
        status.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
        status.name = "mars_cam/points_nav_evidence";
        status.hardware_id = this->get_fully_qualified_name();
        status.message = evidence_enabled_ ? "evidence_enabled" : "evidence_disabled_passthrough";
        auto append = [&status](const std::string& key, const std::string& value) {
            diagnostic_msgs::msg::KeyValue kv;
            kv.key = key;
            kv.value = value;
            status.values.push_back(std::move(kv));
        };
        auto append_size = [&append](const std::string& key, size_t value) { append(key, std::to_string(value)); };
        auto append_float = [&append](const std::string& key, double value) {
            std::ostringstream os;
            os << std::fixed << std::setprecision(4) << value;
            append(key, os.str());
        };

        append("evidence_enabled", evidence_enabled_ ? "true" : "false");
        append("confidence_map_available", conf_scale > 0.0f ? "true" : "false");
        append_float("confidence_threshold_norm", static_cast<double>(confidence_threshold_) / 65535.0);
        append_size("observations", evidence_enabled_ ? evidence_stats.observations : observations.size());
        append_size("voxels_seen", evidence_enabled_ ? evidence_stats.voxels_seen : 0);
        append_size("voxels_supported", evidence_enabled_ ? evidence_stats.voxels_supported : 0);
        append_size("confirmed", evidence_enabled_ ? evidence_stats.confirmed : 0);
        append_size("tracked", evidence_enabled_ ? evidence_stats.tracked : 0);
        append_size("published_points", marks.size());
        append_float("mean_weight", weighted_points ? weight_sum / static_cast<double>(weighted_points) : 0.0);
        append_float("mean_match_confidence", weighted_points ? match_confidence_sum / static_cast<double>(weighted_points) : 0.0);
        append_float("min_match_confidence", weighted_points ? match_confidence_min : 0.0);
        append_float("max_match_confidence", weighted_points ? match_confidence_max : 0.0);
        append_float("mean_range_confidence", weighted_points ? range_confidence_sum / static_cast<double>(weighted_points) : 0.0);
        append_float("dt_sec", evidence_enabled_ ? dt : 0.0);
        append_size("weight_samples", weighted_points);

        diagnostic_msgs::msg::DiagnosticArray diag;
        diag.header.stamp = ts;
        diag.status.push_back(std::move(status));
        pointcloud_nav_stats_pub_->publish(std::move(diag));
    }

    // Published in nav_frame_, NOT the evidence frame. STVL derives an
    // observation's sensor origin from the cloud's frame, so an odom-stamped
    // cloud would put that origin at the odom origin — and obstacle_range would
    // then reject everything once the robot drove away from where odom started.
    const cv::Matx33f odom_to_base = R_odom.t();
    auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
    cloud->header.stamp = ts;
    cloud->header.frame_id = nav_frame_;
    cloud->height = 1;
    cloud->width = static_cast<uint32_t>(marks.size());
    cloud->is_dense = true;
    cloud->is_bigendian = false;

    sensor_msgs::PointCloud2Modifier mod(*cloud);
    mod.setPointCloud2FieldsByString(1, "xyz");
    mod.resize(marks.size());

    sensor_msgs::PointCloud2Iterator<float> ix(*cloud, "x");
    sensor_msgs::PointCloud2Iterator<float> iy(*cloud, "y");
    sensor_msgs::PointCloud2Iterator<float> iz(*cloud, "z");
    for (const auto& m : marks) {
        const cv::Vec3f in_base = odom_to_base * (cv::Vec3f(m[0], m[1], m[2]) - t_odom);
        *ix = in_base[0];
        *iy = in_base[1];
        *iz = in_base[2];
        ++ix;
        ++iy;
        ++iz;
    }

    pointcloud_nav_pub_->publish(std::move(cloud));
}

}  // namespace mars_cam
