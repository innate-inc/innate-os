#include "maurice_cam/main_camera_info.hpp"
#include <nlohmann/json.hpp>
#include <fstream>
#include <chrono>
#include <iomanip>
#include <sstream>

using namespace std::chrono_literals;

namespace maurice_cam
{

MainCameraInfo::MainCameraInfo(const rclcpp::NodeOptions & options)
: Node("main_camera_info", options)
{
  // Declare parameters with defaults
  this->declare_parameter<std::string>("data_directory", "/home/jetson1/innate-os/data");
  this->declare_parameter<std::string>("left_image_topic", "/mars/main_camera/left/image_raw");
  this->declare_parameter<std::string>("right_image_topic", "/mars/main_camera/right/image_raw");
  this->declare_parameter<std::string>("left_info_topic", "/mars/main_camera/left/camera_info");
  this->declare_parameter<std::string>("right_info_topic", "/mars/main_camera/right/camera_info");
  this->declare_parameter<std::string>("left_frame_id", "camera_optical_frame");
  this->declare_parameter<std::string>("right_frame_id", "right_camera_optical_frame");

  // Get parameter values
  data_directory_ = this->get_parameter("data_directory").as_string();
  left_image_topic_ = this->get_parameter("left_image_topic").as_string();
  right_image_topic_ = this->get_parameter("right_image_topic").as_string();
  left_info_topic_ = this->get_parameter("left_info_topic").as_string();
  right_info_topic_ = this->get_parameter("right_info_topic").as_string();
  left_frame_id_ = this->get_parameter("left_frame_id").as_string();
  right_frame_id_ = this->get_parameter("right_frame_id").as_string();

  RCLCPP_INFO(this->get_logger(), "=== Maurice Main Camera Info ===");
  RCLCPP_INFO(this->get_logger(), "Data directory: %s", data_directory_.c_str());
  RCLCPP_INFO(this->get_logger(), "Left info topic: %s", left_info_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Right info topic: %s", right_info_topic_.c_str());

  // Find calibration config directory and load calibration
  try {
    auto calib_dir = findCalibrationConfigDir();
    calib_file_path_ = calib_dir / "stereo_calib.yaml";
    
    if (!loadCalibration(calib_file_path_)) {
      throw std::runtime_error("Failed to load calibration from: " + calib_file_path_.string());
    }
    calibration_loaded_ = true;
    RCLCPP_INFO(this->get_logger(), "Loaded calibration from: %s", calib_file_path_.string().c_str());
    RCLCPP_INFO(this->get_logger(), "Calibration resolution: %dx%d", image_width_, image_height_);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(this->get_logger(), "Calibration error: %s", e.what());
    throw;
  }

  // Build camera info messages
  left_info_msg_ = buildCameraInfo(K1_, D1_, R1_, P1_, left_frame_id_, false);
  right_info_msg_ = buildCameraInfo(K2_, D2_, R2_, P2_, right_frame_id_, true);  // Negate P[0,3] for ROS convention

  // Create publishers
  left_info_pub_ = this->create_publisher<sensor_msgs::msg::CameraInfo>(
    left_info_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  right_info_pub_ = this->create_publisher<sensor_msgs::msg::CameraInfo>(
    right_info_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort)
  );

  // Create services for setting camera info (standard ROS2 interface)
  set_left_info_srv_ = this->create_service<sensor_msgs::srv::SetCameraInfo>(
    "/mars/main_camera/left/set_camera_info",
    std::bind(&MainCameraInfo::handleSetLeftCameraInfo, this, 
              std::placeholders::_1, std::placeholders::_2)
  );

  set_right_info_srv_ = this->create_service<sensor_msgs::srv::SetCameraInfo>(
    "/mars/main_camera/right/set_camera_info",
    std::bind(&MainCameraInfo::handleSetRightCameraInfo, this, 
              std::placeholders::_1, std::placeholders::_2)
  );

  RCLCPP_INFO(this->get_logger(), "Services: /mars/main_camera/{left,right}/set_camera_info");

  // Create image subscriptions to sync camera_info with images
  left_image_sub_ = this->create_subscription<sensor_msgs::msg::Image>(
    left_image_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort),
    std::bind(&MainCameraInfo::leftImageCallback, this, std::placeholders::_1)
  );

  right_image_sub_ = this->create_subscription<sensor_msgs::msg::Image>(
    right_image_topic_,
    rclcpp::SensorDataQoS().reliability(rclcpp::ReliabilityPolicy::BestEffort),
    std::bind(&MainCameraInfo::rightImageCallback, this, std::placeholders::_1)
  );

  RCLCPP_INFO(this->get_logger(), "Subscribed to: %s, %s", left_image_topic_.c_str(), right_image_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "Publishing camera_info synced to image timestamps");
  RCLCPP_INFO(this->get_logger(), "Main Camera Info node initialized successfully");
}

MainCameraInfo::~MainCameraInfo()
{
  RCLCPP_INFO(this->get_logger(), "Shutting down Main Camera Info node");
}

std::filesystem::path MainCameraInfo::findCalibrationConfigDir()
{
  std::filesystem::path data_path(data_directory_);
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
        RCLCPP_INFO(this->get_logger(), "Found robot model: %s", robot_model.c_str());
      }
    } catch (const std::exception& e) {
      RCLCPP_WARN(this->get_logger(), "Could not parse robot_info.json: %s", e.what());
    }
  }

  // Look for calibration_config_* directories
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (entry.is_directory()) {
      std::string dirname = entry.path().filename().string();
      if (dirname.find("calibration_config") != std::string::npos) {
        // If we have a robot model, prefer matching directory
        if (!robot_model.empty() && dirname.find(robot_model) != std::string::npos) {
          RCLCPP_INFO(this->get_logger(), "Found matching calibration dir: %s", dirname.c_str());
          return entry.path();
        }
        // Otherwise use the first calibration_config directory found
        if (robot_model.empty()) {
          RCLCPP_INFO(this->get_logger(), "Using calibration dir: %s", dirname.c_str());
          return entry.path();
        }
      }
    }
  }

  // If we have a robot model but didn't find exact match, use any calibration_config dir
  for (const auto& entry : std::filesystem::directory_iterator(data_path)) {
    if (entry.is_directory()) {
      std::string dirname = entry.path().filename().string();
      if (dirname.find("calibration_config") != std::string::npos) {
        RCLCPP_WARN(this->get_logger(), "No exact match for model %s, using: %s", 
                    robot_model.c_str(), dirname.c_str());
        return entry.path();
      }
    }
  }

  throw std::runtime_error("No calibration_config directory found in: " + data_directory_);
}

bool MainCameraInfo::loadCalibration(const std::filesystem::path& calib_path)
{
  if (!std::filesystem::exists(calib_path)) {
    RCLCPP_ERROR(this->get_logger(), "Calibration file not found: %s", calib_path.string().c_str());
    return false;
  }

  cv::FileStorage fs(calib_path.string(), cv::FileStorage::READ);
  if (!fs.isOpened()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to open calibration file: %s", calib_path.string().c_str());
    return false;
  }

  // Read calibration parameters
  fs["K1"] >> K1_;
  fs["D1"] >> D1_;
  fs["K2"] >> K2_;
  fs["D2"] >> D2_;
  fs["R"] >> R_;
  fs["T"] >> T_;
  fs["R1"] >> R1_;
  fs["R2"] >> R2_;
  fs["P1"] >> P1_;
  fs["P2"] >> P2_;
  fs["Q"] >> Q_;

  // Read calibration image dimensions
  fs["image_width"] >> image_width_;
  fs["image_height"] >> image_height_;

  fs.release();

  // Validate required matrices
  if (K1_.empty() || K2_.empty() || R1_.empty() || R2_.empty() || 
      P1_.empty() || P2_.empty()) {
    RCLCPP_ERROR(this->get_logger(), "Missing required calibration matrices");
    return false;
  }

  RCLCPP_INFO(this->get_logger(), "Loaded calibration: %dx%d", image_width_, image_height_);

  return true;
}

sensor_msgs::msg::CameraInfo MainCameraInfo::buildCameraInfo(
    const cv::Mat& K, const cv::Mat& D, 
    const cv::Mat& R, const cv::Mat& P,
    const std::string& frame_id,
    bool negate_tx)
{
  sensor_msgs::msg::CameraInfo info;
  
  info.header.frame_id = frame_id;
  info.height = image_height_;
  info.width = image_width_;
  info.distortion_model = "plumb_bob";

  // D - distortion coefficients (5 elements for plumb_bob: k1, k2, t1, t2, k3)
  // OpenCV can return D as either 1xN or Nx1, so use linear indexing
  info.d.resize(5);
  if (!D.empty() && D.total() >= 5) {
    const double* d_ptr = D.ptr<double>();
    for (int i = 0; i < 5; i++) {
      info.d[i] = d_ptr[i];
    }
  }

  // K - intrinsic camera matrix (3x3, row-major -> 9 elements)
  // For raw images: [fx 0 cx; 0 fy cy; 0 0 1]
  if (!K.empty()) {
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        info.k[i * 3 + j] = K.at<double>(i, j);
      }
    }
  }

  // R - rectification matrix (3x3, row-major -> 9 elements)
  // Rotation to align camera to ideal stereo image plane
  if (!R.empty()) {
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        info.r[i * 3 + j] = R.at<double>(i, j);
      }
    }
  }

  // P - projection matrix (3x4, row-major -> 12 elements)
  // OpenCV convention: P2[0,3] = +fx' * baseline (positive)
  // ROS convention:    P2[0,3] = -fx' * baseline (negative)
  // For left camera: Tx = 0, so no conversion needed
  // For right camera: negate P[0,3] to convert OpenCV -> ROS convention
  if (!P.empty()) {
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 4; j++) {
        double value = P.at<double>(i, j);
        // Negate P[0,3] (Tx) for right camera to match ROS convention
        if (negate_tx && i == 0 && j == 3) {
          value = -value;
        }
        info.p[i * 4 + j] = value;
      }
    }
  }

  // Binning: 0 means no subsampling (same as 1)
  info.binning_x = 0;
  info.binning_y = 0;

  // ROI: all zeros means full resolution (roi.width = width, roi.height = height)
  // Explicitly set for clarity
  info.roi.x_offset = 0;
  info.roi.y_offset = 0;
  info.roi.width = 0;   // 0 = full width
  info.roi.height = 0;  // 0 = full height
  info.roi.do_rectify = false;  // Images are not pre-rectified

  return info;
}

void MainCameraInfo::leftImageCallback(const sensor_msgs::msg::Image::ConstSharedPtr& msg)
{
  if (!calibration_loaded_) {
    return;
  }

  // Publish left camera_info with same timestamp as image
  left_info_msg_.header.stamp = msg->header.stamp;
  left_info_pub_->publish(left_info_msg_);
}

void MainCameraInfo::rightImageCallback(const sensor_msgs::msg::Image::ConstSharedPtr& msg)
{
  if (!calibration_loaded_) {
    return;
  }

  // Publish right camera_info with same timestamp as image
  right_info_msg_.header.stamp = msg->header.stamp;
  right_info_pub_->publish(right_info_msg_);
}

void MainCameraInfo::handleSetLeftCameraInfo(
    const std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Request> request,
    std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Response> response)
{
  RCLCPP_INFO(this->get_logger(), "Received set_camera_info request for LEFT camera");

  try {
    // Update left camera info
    if (saveCalibration(&request->camera_info, nullptr)) {
      // Reload calibration
      if (loadCalibration(calib_file_path_)) {
        left_info_msg_ = buildCameraInfo(K1_, D1_, R1_, P1_, left_frame_id_);
        right_info_msg_ = buildCameraInfo(K2_, D2_, R2_, P2_, right_frame_id_);
        
        response->success = true;
        response->status_message = "Left camera calibration updated successfully";
        RCLCPP_INFO(this->get_logger(), "Left camera calibration updated");
      } else {
        response->success = false;
        response->status_message = "Failed to reload calibration after save";
      }
    } else {
      response->success = false;
      response->status_message = "Failed to save calibration";
    }
  } catch (const std::exception& e) {
    response->success = false;
    response->status_message = std::string("Exception: ") + e.what();
    RCLCPP_ERROR(this->get_logger(), "Error setting left camera info: %s", e.what());
  }
}

void MainCameraInfo::handleSetRightCameraInfo(
    const std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Request> request,
    std::shared_ptr<sensor_msgs::srv::SetCameraInfo::Response> response)
{
  RCLCPP_INFO(this->get_logger(), "Received set_camera_info request for RIGHT camera");

  try {
    // Update right camera info
    if (saveCalibration(nullptr, &request->camera_info)) {
      // Reload calibration
      if (loadCalibration(calib_file_path_)) {
        left_info_msg_ = buildCameraInfo(K1_, D1_, R1_, P1_, left_frame_id_);
        right_info_msg_ = buildCameraInfo(K2_, D2_, R2_, P2_, right_frame_id_);
        
        response->success = true;
        response->status_message = "Right camera calibration updated successfully";
        RCLCPP_INFO(this->get_logger(), "Right camera calibration updated");
      } else {
        response->success = false;
        response->status_message = "Failed to reload calibration after save";
      }
    } else {
      response->success = false;
      response->status_message = "Failed to save calibration";
    }
  } catch (const std::exception& e) {
    response->success = false;
    response->status_message = std::string("Exception: ") + e.what();
    RCLCPP_ERROR(this->get_logger(), "Error setting right camera info: %s", e.what());
  }
}

std::string MainCameraInfo::backupCalibration(const std::filesystem::path& calib_path)
{
  if (!std::filesystem::exists(calib_path)) {
    return "";
  }

  // Generate timestamped backup filename
  auto now = std::chrono::system_clock::now();
  auto time_t = std::chrono::system_clock::to_time_t(now);
  std::stringstream ss;
  ss << std::put_time(std::localtime(&time_t), "%Y%m%d_%H%M%S");
  
  std::filesystem::path backup_path = calib_path;
  backup_path += ".backup_" + ss.str();

  try {
    std::filesystem::copy_file(calib_path, backup_path);
    RCLCPP_INFO(this->get_logger(), "Backed up calibration to: %s", backup_path.string().c_str());
    return backup_path.string();
  } catch (const std::exception& e) {
    RCLCPP_ERROR(this->get_logger(), "Failed to backup calibration: %s", e.what());
    return "";
  }
}

bool MainCameraInfo::saveCalibration(
    const sensor_msgs::msg::CameraInfo* left_info,
    const sensor_msgs::msg::CameraInfo* right_info)
{
  // Backup existing calibration
  std::string backup_path = backupCalibration(calib_file_path_);
  if (std::filesystem::exists(calib_file_path_) && backup_path.empty()) {
    RCLCPP_ERROR(this->get_logger(), "Failed to backup existing calibration, aborting save");
    return false;
  }

  // Update matrices from CameraInfo messages
  if (left_info) {
    // Update K1
    K1_ = cv::Mat(3, 3, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        K1_.at<double>(i, j) = left_info->k[i * 3 + j];
      }
    }
    
    // Update D1
    D1_ = cv::Mat(1, 5, CV_64F);
    for (int i = 0; i < 5 && i < static_cast<int>(left_info->d.size()); i++) {
      D1_.at<double>(0, i) = left_info->d[i];
    }
    
    // Update R1
    R1_ = cv::Mat(3, 3, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        R1_.at<double>(i, j) = left_info->r[i * 3 + j];
      }
    }
    
    // Update P1
    P1_ = cv::Mat(3, 4, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 4; j++) {
        P1_.at<double>(i, j) = left_info->p[i * 4 + j];
      }
    }
    
    // Update image dimensions
    image_width_ = left_info->width;
    image_height_ = left_info->height;
  }

  if (right_info) {
    // Update K2
    K2_ = cv::Mat(3, 3, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        K2_.at<double>(i, j) = right_info->k[i * 3 + j];
      }
    }
    
    // Update D2
    D2_ = cv::Mat(1, 5, CV_64F);
    for (int i = 0; i < 5 && i < static_cast<int>(right_info->d.size()); i++) {
      D2_.at<double>(0, i) = right_info->d[i];
    }
    
    // Update R2
    R2_ = cv::Mat(3, 3, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        R2_.at<double>(i, j) = right_info->r[i * 3 + j];
      }
    }
    
    // Update P2
    P2_ = cv::Mat(3, 4, CV_64F);
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 4; j++) {
        P2_.at<double>(i, j) = right_info->p[i * 4 + j];
      }
    }
    
    // Update image dimensions (if not already set)
    if (!left_info) {
      image_width_ = right_info->width;
      image_height_ = right_info->height;
    }
  }

  // Save calibration using OpenCV FileStorage (matching existing format)
  try {
    cv::FileStorage fs(calib_file_path_.string(), cv::FileStorage::WRITE);
    if (!fs.isOpened()) {
      RCLCPP_ERROR(this->get_logger(), "Failed to open file for writing: %s", 
                   calib_file_path_.string().c_str());
      return false;
    }
    
    // Write in same order as existing calibration file
    fs << "model" << "pinhole";
    fs << "image_width" << image_width_;
    fs << "image_height" << image_height_;
    fs << "K1" << K1_;
    fs << "D1" << D1_;
    fs << "K2" << K2_;
    fs << "D2" << D2_;
    fs << "R" << R_;
    fs << "T" << T_;
    fs << "R1" << R1_;
    fs << "R2" << R2_;
    fs << "P1" << P1_;
    fs << "P2" << P2_;
    fs << "Q" << Q_;
    
    fs.release();

    RCLCPP_INFO(this->get_logger(), "Saved calibration to: %s", calib_file_path_.string().c_str());
    return true;
  } catch (const std::exception& e) {
    RCLCPP_ERROR(this->get_logger(), "Error saving calibration: %s", e.what());
    return false;
  }
}

} // namespace maurice_cam

// Register the component
RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::MainCameraInfo)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  
  try {
    auto node = std::make_shared<maurice_cam::MainCameraInfo>();
    rclcpp::spin(node);
  } catch (const std::exception& e) {
    RCLCPP_ERROR(rclcpp::get_logger("main"), "Exception: %s", e.what());
    return 1;
  }
  
  rclcpp::shutdown();
  return 0;
}
#endif
