#include "maurice_cam/stereo_calibration.hpp"
#include <nlohmann/json.hpp>
#include <fstream>
#include <iostream>

namespace maurice_cam
{

// ─────────────────────────────────────────────────────────────────────────────
// Factory: auto-discover calibration dir then load
// ─────────────────────────────────────────────────────────────────────────────
std::shared_ptr<StereoCalibration> StereoCalibration::load(const std::string& data_directory)
{
  auto calib_dir = findCalibrationConfigDir(data_directory);
  auto calib_file = calib_dir / "stereo_calib.yaml";
  return loadFromFile(calib_file);
}

// ─────────────────────────────────────────────────────────────────────────────
// Factory: load from explicit YAML path
// ─────────────────────────────────────────────────────────────────────────────
std::shared_ptr<StereoCalibration> StereoCalibration::loadFromFile(const std::filesystem::path& yaml_path)
{
  if (!std::filesystem::exists(yaml_path)) {
    throw std::runtime_error("Calibration file not found: " + yaml_path.string());
  }

  cv::FileStorage fs(yaml_path.string(), cv::FileStorage::READ);
  if (!fs.isOpened()) {
    throw std::runtime_error("Failed to open calibration file: " + yaml_path.string());
  }

  // Version check — file must declare version 2
  int version = 0;
  fs["version"] >> version;
  if (version != 2) {
    throw std::runtime_error("Calibration file version must be 2, got " + std::to_string(version) + ": " + yaml_path.string());
  }

  auto cal = std::shared_ptr<StereoCalibration>(new StereoCalibration());
  cal->file_path_ = yaml_path;

  fs["K1"] >> cal->K1_;
  fs["D1"] >> cal->D1_;
  fs["K2"] >> cal->K2_;
  fs["D2"] >> cal->D2_;
  fs["R"]  >> cal->R_;
  fs["T"]  >> cal->T_;
  fs["R1"] >> cal->R1_;
  fs["R2"] >> cal->R2_;
  fs["P1"] >> cal->P1_;
  fs["P2"] >> cal->P2_;
  fs["Q"]  >> cal->Q_;

  fs["image_width"]  >> cal->calib_width_;
  fs["image_height"] >> cal->calib_height_;

  fs.release();

  // Validate essential matrices
  if (cal->K1_.empty() || cal->K2_.empty() ||
      cal->R1_.empty() || cal->R2_.empty() ||
      cal->P1_.empty() || cal->P2_.empty() ||
      cal->Q_.empty()) {
    throw std::runtime_error("Missing required calibration matrices in: " + yaml_path.string());
  }

  // Extract baseline and focal length from Q matrix
  // Q[2,3] = focal length, Q[3,2] = -1/Tx where Tx = baseline
  cal->focal_length_ = cal->Q_.at<double>(2, 3);
  double neg_inv_tx = cal->Q_.at<double>(3, 2);
  if (std::abs(neg_inv_tx) > 1e-6) {
    cal->baseline_ = std::abs(1.0 / neg_inv_tx);
  } else {
    cal->baseline_ = std::abs(cal->T_.at<double>(0, 0));
  }

  return cal;
}

// ─────────────────────────────────────────────────────────────────────────────
// CameraInfo builders
// ─────────────────────────────────────────────────────────────────────────────
sensor_msgs::msg::CameraInfo StereoCalibration::buildLeftCameraInfo(const std::string& frame_id) const
{
  return buildCameraInfo(K1_, D1_, R1_, P1_, frame_id, /*negate_tx=*/false);
}

sensor_msgs::msg::CameraInfo StereoCalibration::buildRightCameraInfo(const std::string& frame_id) const
{
  return buildCameraInfo(K2_, D2_, R2_, P2_, frame_id, /*negate_tx=*/true);
}

sensor_msgs::msg::CameraInfo StereoCalibration::buildCameraInfo(
    const cv::Mat& K, const cv::Mat& D,
    const cv::Mat& R, const cv::Mat& P,
    const std::string& frame_id,
    bool negate_tx) const
{
  sensor_msgs::msg::CameraInfo info;

  info.header.frame_id = frame_id;
  info.height = calib_height_;
  info.width  = calib_width_;
  info.distortion_model = "plumb_bob";

  // D — distortion coefficients (k1, k2, t1, t2, k3)
  info.d.resize(5);
  if (!D.empty() && D.total() >= 5) {
    const double* d_ptr = D.ptr<double>();
    for (int i = 0; i < 5; i++) {
      info.d[i] = d_ptr[i];
    }
  }

  // K — 3×3 intrinsic matrix
  if (!K.empty()) {
    for (int i = 0; i < 3; i++)
      for (int j = 0; j < 3; j++)
        info.k[i * 3 + j] = K.at<double>(i, j);
  }

  // R — 3×3 rectification rotation
  if (!R.empty()) {
    for (int i = 0; i < 3; i++)
      for (int j = 0; j < 3; j++)
        info.r[i * 3 + j] = R.at<double>(i, j);
  }

  // P — 3×4 projection matrix
  // OpenCV: P2[0,3] = +fx'·baseline   ROS: P2[0,3] = −fx'·baseline
  if (!P.empty()) {
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 4; j++) {
        double value = P.at<double>(i, j);
        if (negate_tx && i == 0 && j == 3)
          value = -value;
        info.p[i * 4 + j] = value;
      }
    }
  }

  info.binning_x = 0;
  info.binning_y = 0;
  info.roi.x_offset = 0;
  info.roi.y_offset = 0;
  info.roi.width  = 0;
  info.roi.height = 0;
  info.roi.do_rectify = false;

  return info;
}

// ─────────────────────────────────────────────────────────────────────────────
// Rectification maps — at calibration resolution
// ─────────────────────────────────────────────────────────────────────────────
void StereoCalibration::getRectificationMaps(
    cv::Mat& map1_left, cv::Mat& map2_left,
    cv::Mat& map1_right, cv::Mat& map2_right) const
{
  cv::initUndistortRectifyMap(K1_, D1_, R1_, P1_,
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_left, map2_left);
  cv::initUndistortRectifyMap(K2_, D2_, R2_, P2_,
                               cv::Size(calib_width_, calib_height_),
                               CV_32FC1, map1_right, map2_right);
}

// ─────────────────────────────────────────────────────────────────────────────
// Rectification maps — at arbitrary resolution (scale K + P)
// ─────────────────────────────────────────────────────────────────────────────
void StereoCalibration::getRectificationMaps(
    int width, int height,
    cv::Mat& map1_left, cv::Mat& map2_left,
    cv::Mat& map1_right, cv::Mat& map2_right) const
{
  double sx = static_cast<double>(width)  / static_cast<double>(calib_width_);
  double sy = static_cast<double>(height) / static_cast<double>(calib_height_);

  // Scale K matrices
  cv::Mat K1s = K1_.clone();
  K1s.at<double>(0, 0) *= sx; K1s.at<double>(0, 2) *= sx;
  K1s.at<double>(1, 1) *= sy; K1s.at<double>(1, 2) *= sy;

  cv::Mat K2s = K2_.clone();
  K2s.at<double>(0, 0) *= sx; K2s.at<double>(0, 2) *= sx;
  K2s.at<double>(1, 1) *= sy; K2s.at<double>(1, 2) *= sy;

  // Scale P matrices (only the 3×3 part and Tx element)
  cv::Mat P1s = P1_.clone();
  P1s.at<double>(0, 0) *= sx; P1s.at<double>(0, 2) *= sx; P1s.at<double>(0, 3) *= sx;
  P1s.at<double>(1, 1) *= sy; P1s.at<double>(1, 2) *= sy;

  cv::Mat P2s = P2_.clone();
  P2s.at<double>(0, 0) *= sx; P2s.at<double>(0, 2) *= sx; P2s.at<double>(0, 3) *= sx;
  P2s.at<double>(1, 1) *= sy; P2s.at<double>(1, 2) *= sy;

  cv::initUndistortRectifyMap(K1s, D1_, R1_, P1s,
                               cv::Size(width, height),
                               CV_32FC1, map1_left, map2_left);
  cv::initUndistortRectifyMap(K2s, D2_, R2_, P2s,
                               cv::Size(width, height),
                               CV_32FC1, map1_right, map2_right);
}

// ─────────────────────────────────────────────────────────────────────────────
// Calibration directory discovery
// ─────────────────────────────────────────────────────────────────────────────
std::filesystem::path StereoCalibration::findCalibrationConfigDir(const std::string& data_directory)
{
  std::filesystem::path data_path(data_directory);
  std::filesystem::path robot_info_path = data_path / "robot_info.json";

  // Try to read robot_info.json to get the robot model
  std::string robot_model;
  if (std::filesystem::exists(robot_info_path)) {
    try {
      std::ifstream file(robot_info_path);
      nlohmann::json robot_info;
      file >> robot_info;
      if (robot_info.contains("model")) {
        robot_model = robot_info["model"].get<std::string>();
      }
    } catch (...) {
      // Ignore parse errors — fall through to directory scan
    }
  }

  // First pass: prefer directory matching robot model
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (!entry.is_directory()) continue;
    std::string dirname = entry.path().filename().string();
    if (dirname.find("calibration_config") == std::string::npos) continue;
    if (!robot_model.empty() && dirname.find(robot_model) != std::string::npos)
      return entry.path();
    if (robot_model.empty())
      return entry.path();
  }

  // Second pass: fall back to any calibration_config dir
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (!entry.is_directory()) continue;
    std::string dirname = entry.path().filename().string();
    if (dirname.find("calibration_config") != std::string::npos)
      return entry.path();
  }

  throw std::runtime_error("No calibration_config directory found in: " + data_directory);
}

} // namespace maurice_cam
