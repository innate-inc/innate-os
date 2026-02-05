// Copyright (c) 2024 Maurice Robotics
// Disparity Median Filter - Applies OpenCV median filter to disparity images

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <stereo_msgs/msg/disparity_image.hpp>
#include <opencv2/opencv.hpp>
#include <cv_bridge/cv_bridge.h>

namespace maurice_cam
{

class DisparityMedianFilter : public rclcpp::Node
{
public:
  explicit DisparityMedianFilter(const rclcpp::NodeOptions & options)
  : Node("disparity_median_filter", options)
  {
    // Declare parameters
    this->declare_parameter<int>("kernel_size", 5);
    kernel_size_ = this->get_parameter("kernel_size").as_int();
    
    // Ensure kernel size is odd
    if (kernel_size_ % 2 == 0) {
      kernel_size_ += 1;
      RCLCPP_WARN(this->get_logger(), "Kernel size must be odd, adjusted to %d", kernel_size_);
    }
    
    RCLCPP_INFO(this->get_logger(), "Disparity Median Filter initialized with kernel_size=%d", kernel_size_);
    
    // Create publisher
    pub_ = this->create_publisher<stereo_msgs::msg::DisparityImage>("disparity", 10);
    
    // Create subscriber
    sub_ = this->create_subscription<stereo_msgs::msg::DisparityImage>(
      "disparity_unfiltered", 10,
      std::bind(&DisparityMedianFilter::callback, this, std::placeholders::_1));
  }

private:
  void callback(const stereo_msgs::msg::DisparityImage::SharedPtr msg)
  {
    auto start = std::chrono::steady_clock::now();
    
    // Convert disparity image to OpenCV Mat
    // DisparityImage uses 32FC1 encoding
    cv::Mat disparity(msg->image.height, msg->image.width, CV_32FC1,
                      const_cast<uint8_t*>(msg->image.data.data()));
    
    // Apply median filter
    // Note: medianBlur only works on CV_8U, CV_16U, CV_16S, or CV_32F for ksize 3 or 5
    // For larger kernels, we need CV_8U
    cv::Mat filtered;
    
    if (kernel_size_ <= 5) {
      // Can use float directly for small kernels
      cv::medianBlur(disparity, filtered, kernel_size_);
    } else {
      // For larger kernels, need to convert to 8-bit
      // Normalize disparity to 0-255 range, apply filter, then scale back
      double min_val, max_val;
      cv::minMaxLoc(disparity, &min_val, &max_val);
      
      cv::Mat disparity_8u;
      disparity.convertTo(disparity_8u, CV_8U, 255.0 / (max_val - min_val + 1e-6), 
                          -min_val * 255.0 / (max_val - min_val + 1e-6));
      
      cv::Mat filtered_8u;
      cv::medianBlur(disparity_8u, filtered_8u, kernel_size_);
      
      // Convert back to float
      filtered_8u.convertTo(filtered, CV_32F, (max_val - min_val) / 255.0, min_val);
    }
    
    // Create output message
    auto out_msg = std::make_unique<stereo_msgs::msg::DisparityImage>(*msg);
    
    // Copy filtered data back
    std::memcpy(out_msg->image.data.data(), filtered.data, 
                filtered.total() * filtered.elemSize());
    
    pub_->publish(std::move(out_msg));
    
    auto end = std::chrono::steady_clock::now();
    double ms = std::chrono::duration<double, std::milli>(end - start).count();
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
      "Median filter applied in %.2f ms", ms);
  }

  rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr pub_;
  rclcpp::Subscription<stereo_msgs::msg::DisparityImage>::SharedPtr sub_;
  int kernel_size_;
};

}  // namespace maurice_cam

RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::DisparityMedianFilter)
