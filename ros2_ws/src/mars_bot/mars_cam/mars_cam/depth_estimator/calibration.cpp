// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Camera-info based calibration: subscribe to left/right CameraInfo topics,
// extract K, D, R, P matrices, compute rectification maps, derive baseline
// and focal length, then kick off VPI initialisation.

#include "mars_cam/stereo_depth_estimator.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <vector>

namespace mars_cam {

// =============================================================================
// Camera info callbacks — collect left/right, (re-)init when data changes
// =============================================================================
static bool cameraInfoEqual(const sensor_msgs::msg::CameraInfo& a, const sensor_msgs::msg::CameraInfo& b) {
    return a.width == b.width && a.height == b.height && a.k == b.k && a.d == b.d && a.r == b.r && a.p == b.p;
}

void StereoDepthEstimator::leftCameraInfoCallback(const sensor_msgs::msg::CameraInfo::ConstSharedPtr& msg) {
    if (left_camera_info_ && cameraInfoEqual(*left_camera_info_, *msg))
        return;
    left_camera_info_ = msg;
    RCLCPP_INFO(this->get_logger(), "Received left camera_info (%dx%d)", msg->width, msg->height);
    if (right_camera_info_)
        initCalibrationFromCameraInfo();
}

void StereoDepthEstimator::rightCameraInfoCallback(const sensor_msgs::msg::CameraInfo::ConstSharedPtr& msg) {
    if (right_camera_info_ && cameraInfoEqual(*right_camera_info_, *msg))
        return;
    right_camera_info_ = msg;
    RCLCPP_INFO(this->get_logger(), "Received right camera_info (%dx%d)", msg->width, msg->height);
    if (left_camera_info_)
        initCalibrationFromCameraInfo();
}

// =============================================================================
// Per-robot camera mount correction
// =============================================================================
// Lives in the runtime data directory beside stereo_calib.yaml, NOT in the
// shipped config: these values are this unit's assembly variation, not a
// setting, and applying a recalibration must not require a rebuild. Written by
// `ros2 run mars_cam ground_plane_check -p write:=true`.
//
// Absent file is normal — an uncalibrated robot falls back to the config
// parameters rather than refusing to start.
void StereoDepthEstimator::loadMountCorrection() {
    namespace fs = std::filesystem;
    std::error_code ec;
    const fs::path data_dir(data_directory_);
    if (!fs::exists(data_dir, ec)) {
        RCLCPP_INFO(this->get_logger(), "Mount correction: no data directory, using config parameters");
        return;
    }

    // Sorted, so two calibration_config directories resolve the same way here
    // as they do in the Python writer.
    std::vector<fs::path> candidates;
    for (const auto& entry : fs::directory_iterator(data_dir, ec)) {
        if (entry.is_directory(ec) && entry.path().filename().string().find("calibration_config") != std::string::npos)
            candidates.push_back(entry.path());
    }
    if (candidates.empty()) {
        RCLCPP_INFO(this->get_logger(), "Mount correction: no calibration_config directory, using config parameters");
        return;
    }
    std::sort(candidates.begin(), candidates.end());

    const fs::path file = candidates.front() / "camera_mount.yaml";
    if (!fs::exists(file, ec)) {
        RCLCPP_INFO(this->get_logger(), "Mount correction: %s absent, using config parameters (pitch %+.3f roll %+.3f)",
                    file.c_str(), mount_pitch_correction_deg_, mount_roll_correction_deg_);
        return;
    }

    cv::FileStorage storage(file.string(), cv::FileStorage::READ);
    if (!storage.isOpened()) {
        RCLCPP_WARN(this->get_logger(), "Mount correction: cannot open %s, using config parameters", file.c_str());
        return;
    }
    int version = 0;
    storage["version"] >> version;
    if (version != 1) {
        RCLCPP_WARN(this->get_logger(), "Mount correction: %s is version %d, expected 1 — ignoring", file.c_str(),
                    version);
        storage.release();
        return;
    }
    double pitch = 0.0, roll = 0.0, height = 0.0, head = 0.0;
    storage["pitch_deg"] >> pitch;
    storage["roll_deg"] >> roll;
    storage["height_m"] >> height;
    storage["head_angle_deg"] >> head;
    storage.release();

    mount_pitch_correction_deg_ = pitch;
    mount_roll_correction_deg_ = roll;
    mount_height_correction_m_ = height;
    RCLCPP_INFO(this->get_logger(),
                "Mount correction from %s: pitch %+.3f deg, roll %+.3f deg, height %+.1f mm (measured at head %+.2f)",
                file.c_str(), pitch, roll, height * 1000.0, head);
}

// =============================================================================
// Rotation from the rectified left frame into camera_optical_frame
// =============================================================================
// Points are back-projected with P1, so they land in the RECTIFIED left frame,
// but the cloud is stamped camera_optical_frame — the unrectified left camera
// in the URDF. Without R1^T the whole cloud is tilted by R1, which the costmap
// sees as the floor sloping away and marks as obstacles.
//
// The mount corrections sit on top for the mechanical error the URDF cannot
// know about (the head servo zero is one hardcoded homing_offset shared across
// robots). Measure them with `ros2 run mars_cam ground_plane_check`.
void StereoDepthEstimator::updateCloudRotation(const cv::Mat& R1) {
    cv::Matx33d rectified_to_optical = cv::Matx33d::eye();
    if (!R1.empty() && R1.rows == 3 && R1.cols == 3) {
        rectified_to_optical = cv::Matx33d(R1.ptr<double>()).t();
    }

    // Optical-frame axes: x right, y down, z forward. A nose-down mount error
    // is a rotation about x; a roll error is a rotation about z.
    const double pitch = mount_pitch_correction_deg_ * CV_PI / 180.0;
    const double roll = mount_roll_correction_deg_ * CV_PI / 180.0;
    const cv::Matx33d pitch_fix(1, 0, 0, 0, std::cos(pitch), -std::sin(pitch), 0, std::sin(pitch), std::cos(pitch));
    const cv::Matx33d roll_fix(std::cos(roll), -std::sin(roll), 0, std::sin(roll), std::cos(roll), 0, 0, 0, 1);

    const cv::Matx33d combined = roll_fix * pitch_fix * rectified_to_optical;
    for (int r = 0; r < 3; ++r)
        for (int c = 0; c < 3; ++c)
            cloud_rotation_(r, c) = static_cast<float>(combined(r, c));

    // How much tilt R1 alone was contributing, so its share of any measured
    // floor slope is visible rather than inferred.
    const double r1_pitch_deg = std::asin(std::clamp(-rectified_to_optical(1, 2), -1.0, 1.0)) * 180.0 / CV_PI;
    const double r1_roll_deg = std::asin(std::clamp(-rectified_to_optical(0, 1), -1.0, 1.0)) * 180.0 / CV_PI;
    RCLCPP_INFO(this->get_logger(), "  Cloud rotation: R1 contributes pitch %+.3f deg, roll %+.3f deg", r1_pitch_deg,
                r1_roll_deg);
    if (mount_pitch_correction_deg_ != 0.0 || mount_roll_correction_deg_ != 0.0) {
        RCLCPP_INFO(this->get_logger(), "  Mount correction: pitch %+.3f deg, roll %+.3f deg",
                    mount_pitch_correction_deg_, mount_roll_correction_deg_);
    }
}

bool StereoDepthEstimator::initCalibrationFromCameraInfo() {
    std::lock_guard<std::mutex> lock(calib_mutex_);

    const auto& left = *left_camera_info_;
    const auto& right = *right_camera_info_;

    // Check if cameras are calibrated (K[0] == 0.0 indicates uncalibrated)
    if (left.k[0] == 0.0 || right.k[0] == 0.0) {
        RCLCPP_WARN(this->get_logger(), "Camera is uncalibrated (K[0] == 0.0). Depth estimation disabled.");
        calibration_loaded_ = false;
        return false;
    }

    calib_width_ = static_cast<int>(left.width);
    calib_height_ = static_cast<int>(left.height);

    // ── Extract matrices from CameraInfo messages ───────────────────────────
    // k/r/p are contiguous row-major doubles — same layout as cv::Mat(CV_64F).
    auto mat33 = [](const auto& a) { return cv::Mat(3, 3, CV_64F, const_cast<double*>(a.data())).clone(); };
    auto mat34 = [](const auto& a) { return cv::Mat(3, 4, CV_64F, const_cast<double*>(a.data())).clone(); };
    auto vecD = [](const auto& v) {
        return cv::Mat(1, static_cast<int>(v.size()), CV_64F, const_cast<double*>(v.data())).clone();
    };

    cv::Mat K1 = mat33(left.k), R1 = mat33(left.r);
    P1_ = mat34(left.p);
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
        RCLCPP_ERROR(this->get_logger(), "Cannot determine baseline from camera_info (P2[0,3] ≈ 0)");
        return false;
    }

    // Restore OpenCV sign convention for P2[0,3] before computing remap tables.
    // OpenCV expects P2[0,3] = +fx'·baseline (positive).
    cv::Mat P2_cv = P2.clone();
    P2_cv.at<double>(0, 3) = std::abs(P2_cv.at<double>(0, 3));

    // ── Compute rectification maps ─────────────────────────────────────────
    cv::initUndistortRectifyMap(K1, D1, R1, P1_, cv::Size(calib_width_, calib_height_), CV_32FC1, map1_left_,
                                map2_left_);
    cv::initUndistortRectifyMap(K2, D2, R2, P2_cv, cv::Size(calib_width_, calib_height_), CV_32FC1, map1_right_,
                                map2_right_);

    updateCloudRotation(R1);

    RCLCPP_INFO(this->get_logger(), "Calibration extracted from camera_info:");
    RCLCPP_INFO(this->get_logger(), "  Resolution: %dx%d", calib_width_, calib_height_);
    RCLCPP_INFO(this->get_logger(), "  Stereo: focal=%.2f px, baseline=%.4f m (%.1f mm)", focal_length_, baseline_,
                baseline_ * 1000.0);

    // ── (Re-)Initialize VPI (remap + SGM) ──────────────────────────────────
    // Tear down existing resources if this is a recalibration.
    if (vpi_initialized_) {
        calibration_loaded_ = false;  // gate processFrame while we reinit
        vpi_initialized_ = false;
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

}  // namespace mars_cam
