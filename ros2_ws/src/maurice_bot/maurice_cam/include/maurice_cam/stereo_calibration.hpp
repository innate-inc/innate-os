#pragma once

#include <memory>
#include <string>
#include <filesystem>
#include <stdexcept>

#include <opencv2/opencv.hpp>
#include "sensor_msgs/msg/camera_info.hpp"

namespace maurice_cam
{

/**
 * @brief Shared stereo-calibration utility — NOT a ROS node.
 *
 * Loads stereo_calib.yaml once and provides:
 *   • Ready-to-publish CameraInfo messages (left / right)
 *   • Rectification maps at calibration or arbitrary resolution
 *   • Accessors for every calibration matrix (K, D, R, T, R1, R2, P1, P2, Q)
 *   • Convenience getters: baseline(), focalLength(), calibWidth(), calibHeight()
 *
 * Designed to be shared among MainCameraDriver, StereoDepthEstimator, etc.,
 * eliminating the duplicated findCalibrationConfigDir / loadCalibration code.
 */
class StereoCalibration
{
public:
  // ── Factory methods ────────────────────────────────────────────────────

  /**
   * @brief Auto-discover calibration directory under `data_directory` and load.
   *
   * Reads robot_info.json → calibration_config_<model>/stereo_calib.yaml.
   * Throws on failure.
   */
  static std::shared_ptr<StereoCalibration> load(const std::string& data_directory);

  /**
   * @brief Load directly from a YAML file path.
   */
  static std::shared_ptr<StereoCalibration> loadFromFile(const std::filesystem::path& yaml_path);

  // ── CameraInfo builders ────────────────────────────────────────────────

  /** Build a sensor_msgs::CameraInfo for the LEFT camera. */
  sensor_msgs::msg::CameraInfo buildLeftCameraInfo(const std::string& frame_id = "camera_optical_frame") const;

  /** Build a sensor_msgs::CameraInfo for the RIGHT camera. */
  sensor_msgs::msg::CameraInfo buildRightCameraInfo(const std::string& frame_id = "right_camera_optical_frame") const;

  // ── Rectification maps ────────────────────────────────────────────────

  /**
   * @brief Compute rectification maps at the calibration resolution.
   * @param[out] map1_left, map2_left   Left remap pair (CV_32FC1)
   * @param[out] map1_right, map2_right Right remap pair (CV_32FC1)
   */
  void getRectificationMaps(
    cv::Mat& map1_left, cv::Mat& map2_left,
    cv::Mat& map1_right, cv::Mat& map2_right) const;

  /**
   * @brief Compute rectification maps at an arbitrary resolution.
   * Scales K and P to the requested size.
   */
  void getRectificationMaps(
    int width, int height,
    cv::Mat& map1_left, cv::Mat& map2_left,
    cv::Mat& map1_right, cv::Mat& map2_right) const;

  // ── Accessors ──────────────────────────────────────────────────────────

  const cv::Mat& K1() const { return K1_; }
  const cv::Mat& D1() const { return D1_; }
  const cv::Mat& K2() const { return K2_; }
  const cv::Mat& D2() const { return D2_; }
  const cv::Mat& R()  const { return R_;  }
  const cv::Mat& T()  const { return T_;  }
  const cv::Mat& R1() const { return R1_; }
  const cv::Mat& R2() const { return R2_; }
  const cv::Mat& P1() const { return P1_; }
  const cv::Mat& P2() const { return P2_; }
  const cv::Mat& Q()  const { return Q_;  }

  double baseline()    const { return baseline_;    }
  double focalLength() const { return focal_length_; }
  int calibWidth()     const { return calib_width_;  }
  int calibHeight()    const { return calib_height_; }

  /** Path to the loaded YAML file. */
  const std::filesystem::path& filePath() const { return file_path_; }

private:
  StereoCalibration() = default;  // Only created via factories

  /** Auto-discover calibration_config_* directory. */
  static std::filesystem::path findCalibrationConfigDir(const std::string& data_directory);

  /** Build CameraInfo for either camera. */
  sensor_msgs::msg::CameraInfo buildCameraInfo(
    const cv::Mat& K, const cv::Mat& D,
    const cv::Mat& R, const cv::Mat& P,
    const std::string& frame_id,
    bool negate_tx) const;

  // Calibration matrices
  cv::Mat K1_, D1_, K2_, D2_;
  cv::Mat R_, T_;
  cv::Mat R1_, R2_, P1_, P2_, Q_;

  int calib_width_{0};
  int calib_height_{0};
  double baseline_{0.0};
  double focal_length_{0.0};

  std::filesystem::path file_path_;
};

} // namespace maurice_cam
