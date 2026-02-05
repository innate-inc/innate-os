// Copyright (c) 2024 Maurice Robotics
// Disparity Edge-Preserving Filter - Domain Transform based spatial filter
// Based on "Domain Transform for Edge-Aware Image and Video Processing" 
// by Eduardo S. L. Gastal and Manuel M. Oliveira (SIGGRAPH 2011)

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <stereo_msgs/msg/disparity_image.hpp>
#include <opencv2/opencv.hpp>
#include <cmath>
#include <limits>

namespace maurice_cam
{

class DisparityEdgePreservingFilter : public rclcpp::Node
{
public:
  explicit DisparityEdgePreservingFilter(const rclcpp::NodeOptions & options)
  : Node("disparity_edge_preserving_filter", options)
  {
    // Declare parameters with descriptions
    // Filter Magnitude: Number of filter iterations [1-5], default 2
    // More iterations = smoother result but more compute
    this->declare_parameter<int>("filter_magnitude", 2);
    
    // Smooth Alpha: EMA factor [0.25-1.0], default 0.5
    // Alpha=1: no filtering, Alpha=0.25: heavy filtering
    // Controls how much the current pixel is blended with neighbors
    this->declare_parameter<double>("smooth_alpha", 0.5);
    
    // Smooth Delta: Edge threshold [1-50], default 20
    // Disparity differences larger than this are treated as edges and preserved
    // Lower = more edges preserved, higher = smoother across more edges
    this->declare_parameter<int>("smooth_delta", 20);
    
    // Hole Filling: Symmetric hole filling mode [0-5], default 0
    // Maps to [none, 2, 4, 8, 16, unlimited] pixel search radius
    // 0=disabled, 1=2px, 2=4px, 3=8px, 4=16px, 5=unlimited
    this->declare_parameter<int>("hole_filling", 0);
    
    // Depth Clamping Parameters (in meters)
    // Disparity is computed from depth using: disparity = f * |T| / depth
    // where f = focal length (pixels), T = baseline (meters)
    
    // Min depth: Objects closer than this are invalidated (set to 0)
    // -1 = disabled (no minimum depth limit)
    this->declare_parameter<double>("min_depth_meters", -1.0);
    
    // Max depth: Objects farther than this are invalidated (set to 0)
    // -1 = disabled (no maximum depth limit)
    this->declare_parameter<double>("max_depth_meters", -1.0);
    
    // Edge Invalidation: Detect edges in disparity and invalidate pixels along them
    // This removes depth discontinuities which cause noise in point clouds
    // 0 = disabled, >0 = width of invalidation band along edges (pixels)
    this->declare_parameter<int>("edge_invalidation_width", 0);
    
    // Canny edge detection thresholds (on disparity gradient magnitude)
    // Lower threshold for hysteresis
    this->declare_parameter<double>("edge_canny_low", 10.0);
    // Upper threshold for hysteresis  
    this->declare_parameter<double>("edge_canny_high", 30.0);
    
    // Speckle Filter: Remove small isolated blobs using cv::filterSpeckles
    // Designed specifically for disparity maps in stereo vision
    // 0 = disabled
    this->declare_parameter<int>("speckle_size", 0);
    
    // Maximum disparity difference for pixels to be considered connected
    // Higher = more lenient connectivity, lower = stricter
    this->declare_parameter<double>("speckle_diff", 1.0);
    
    // Load parameters
    filter_magnitude_ = this->get_parameter("filter_magnitude").as_int();
    smooth_alpha_ = this->get_parameter("smooth_alpha").as_double();
    smooth_delta_ = this->get_parameter("smooth_delta").as_int();
    hole_filling_ = this->get_parameter("hole_filling").as_int();
    min_depth_meters_ = this->get_parameter("min_depth_meters").as_double();
    max_depth_meters_ = this->get_parameter("max_depth_meters").as_double();
    edge_invalidation_width_ = this->get_parameter("edge_invalidation_width").as_int();
    edge_canny_low_ = this->get_parameter("edge_canny_low").as_double();
    edge_canny_high_ = this->get_parameter("edge_canny_high").as_double();
    speckle_size_ = this->get_parameter("speckle_size").as_int();
    speckle_diff_ = this->get_parameter("speckle_diff").as_double();
    
    // Clamp parameters to valid ranges
    filter_magnitude_ = std::clamp(filter_magnitude_, 1, 5);
    smooth_alpha_ = std::clamp(smooth_alpha_, 0.25, 1.0);
    smooth_delta_ = std::clamp(smooth_delta_, 1, 50);
    hole_filling_ = std::clamp(hole_filling_, 0, 5);
    edge_invalidation_width_ = std::max(0, edge_invalidation_width_);
    speckle_size_ = std::max(0, speckle_size_);
    speckle_diff_ = std::max(0.0, speckle_diff_);
    
    // Convert hole_filling to actual pixel radius
    const int hole_fill_map[] = {0, 2, 4, 8, 16, 9999};  // 9999 = unlimited
    hole_fill_radius_ = hole_fill_map[hole_filling_];
    
    RCLCPP_INFO(this->get_logger(), 
      "Edge-Preserving Filter initialized: magnitude=%d, alpha=%.2f, delta=%d, hole_fill=%d (radius=%d)",
      filter_magnitude_, smooth_alpha_, smooth_delta_, hole_filling_, hole_fill_radius_);
    RCLCPP_INFO(this->get_logger(),
      "Depth clamping: min_depth=%.2fm, max_depth=%.2fm (-1=disabled)",
      min_depth_meters_, max_depth_meters_);
    if (edge_invalidation_width_ > 0) {
      RCLCPP_INFO(this->get_logger(),
        "Edge invalidation enabled: width=%d px, Canny thresholds=[%.1f, %.1f]",
        edge_invalidation_width_, edge_canny_low_, edge_canny_high_);
    }
    
    // Create publisher
    pub_ = this->create_publisher<stereo_msgs::msg::DisparityImage>("disparity", 10);
    
    // Create subscriber
    sub_ = this->create_subscription<stereo_msgs::msg::DisparityImage>(
      "disparity_unfiltered", 10,
      std::bind(&DisparityEdgePreservingFilter::callback, this, std::placeholders::_1));
  }

private:
  void callback(const stereo_msgs::msg::DisparityImage::SharedPtr msg)
  {
    auto start = std::chrono::steady_clock::now();
    
    // Convert disparity image to OpenCV Mat (32FC1)
    cv::Mat disparity(msg->image.height, msg->image.width, CV_32FC1,
                      const_cast<uint8_t*>(msg->image.data.data()));
    
    cv::Mat filtered = disparity.clone();
    
    // Apply hole filling first if enabled
    if (hole_fill_radius_ > 0) {
      fillHoles(filtered);
    }
    
    // Apply depth clamping (invalidate pixels outside depth range)
    int clamped_count = 0;
    if (min_depth_meters_ > 0 || max_depth_meters_ > 0) {
      clamped_count = clampByDepth(filtered, msg->f, msg->t);
    }
    
    // Apply edge invalidation (detect edges and invalidate pixels along them)
    int edge_invalidated_count = 0;
    if (edge_invalidation_width_ > 0) {
      edge_invalidated_count = invalidateEdges(filtered);
    }
    
    // Apply speckle filter (remove small isolated blobs)
    int speckle_removed_count = 0;
    if (speckle_size_ > 0) {
      speckle_removed_count = filterSpeckles(filtered);
    }
    
    // Apply edge-preserving filter (domain transform style)
    for (int iter = 0; iter < filter_magnitude_; ++iter) {
      // Horizontal pass (left to right, then right to left)
      domainTransformPass(filtered, true);
      // Vertical pass (top to bottom, then bottom to top)
      domainTransformPass(filtered, false);
    }
    
    // Create output message
    auto out_msg = std::make_unique<stereo_msgs::msg::DisparityImage>(*msg);
    std::memcpy(out_msg->image.data.data(), filtered.data, 
                filtered.total() * filtered.elemSize());
    
    pub_->publish(std::move(out_msg));
    
    auto end = std::chrono::steady_clock::now();
    double ms = std::chrono::duration<double, std::milli>(end - start).count();
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
      "Edge-preserving filter: %.2f ms, depth_clamped=%d, edge_inv=%d, speckle=%d", 
      ms, clamped_count, edge_invalidated_count, speckle_removed_count);
  }
  
  // Detect edges in disparity image using Canny, dilate them, and invalidate
  // Returns number of pixels invalidated
  int invalidateEdges(cv::Mat& disparity)
  {
    const int rows = disparity.rows;
    const int cols = disparity.cols;
    
    // Normalize disparity to 8-bit for edge detection
    // Find valid min/max for proper normalization
    double min_val, max_val;
    cv::minMaxLoc(disparity, &min_val, &max_val);
    
    if (max_val <= 0) {
      return 0;  // No valid disparities
    }
    
    // Convert to 8-bit grayscale
    cv::Mat disp_8u;
    disparity.convertTo(disp_8u, CV_8UC1, 255.0 / max_val);
    
    // Apply Gaussian blur to reduce noise before edge detection
    cv::Mat blurred;
    cv::GaussianBlur(disp_8u, blurred, cv::Size(3, 3), 0.8);
    
    // Detect edges using Canny
    cv::Mat edges;
    cv::Canny(blurred, edges, edge_canny_low_, edge_canny_high_);
    
    // Dilate edges to create invalidation band
    // edge_invalidation_width_ is the total width, so kernel size is width
    // For width=3, we want 1 pixel on each side of the edge
    if (edge_invalidation_width_ > 1) {
      int kernel_size = edge_invalidation_width_;
      // Ensure kernel size is odd
      if (kernel_size % 2 == 0) kernel_size++;
      cv::Mat kernel = cv::getStructuringElement(cv::MORPH_ELLIPSE, 
                                                  cv::Size(kernel_size, kernel_size));
      cv::dilate(edges, edges, kernel);
    }
    
    // Invalidate disparity pixels where edges are detected
    int invalidated = 0;
    for (int y = 0; y < rows; ++y) {
      float* disp_row = disparity.ptr<float>(y);
      const uchar* edge_row = edges.ptr<uchar>(y);
      
      for (int x = 0; x < cols; ++x) {
        // Only invalidate if edge detected AND disparity was valid
        if (edge_row[x] > 0 && disp_row[x] > 0) {
          disp_row[x] = 0.0f;
          ++invalidated;
        }
      }
    }
    
    return invalidated;
  }
  
  // Filter speckles (small isolated blobs) using OpenCV's filterSpeckles
  // Designed specifically for disparity maps
  // Returns approximate number of pixels removed
  int filterSpeckles(cv::Mat& disparity)
  {
    // Count valid pixels before filtering
    int valid_before = 0;
    for (int y = 0; y < disparity.rows; ++y) {
      const float* row = disparity.ptr<float>(y);
      for (int x = 0; x < disparity.cols; ++x) {
        if (row[x] > 0) valid_before++;
      }
    }
    
    // cv::filterSpeckles requires 16-bit signed input
    // Scale disparity to fixed-point: multiply by 16 (standard for OpenCV stereo)
    const float scale = 16.0f;
    cv::Mat disp_16s;
    disparity.convertTo(disp_16s, CV_16SC1, scale);
    
    // Apply speckle filter
    // newVal = 0 (set speckles to invalid)
    // maxSpeckleSize = speckle_size_ (max pixels in a speckle)
    // maxDiff = speckle_diff_ * scale (max disparity difference for connectivity)
    cv::filterSpeckles(disp_16s, 0, speckle_size_, 
                       static_cast<double>(speckle_diff_ * scale));
    
    // Convert back to 32-bit float
    disp_16s.convertTo(disparity, CV_32FC1, 1.0 / scale);
    
    // Count valid pixels after filtering
    int valid_after = 0;
    for (int y = 0; y < disparity.rows; ++y) {
      const float* row = disparity.ptr<float>(y);
      for (int x = 0; x < disparity.cols; ++x) {
        if (row[x] > 0) valid_after++;
      }
    }
    
    return valid_before - valid_after;
  }
  
  // Clamp disparity by depth range - invalidate pixels outside [min_depth, max_depth]
  // Returns number of pixels invalidated
  int clampByDepth(cv::Mat& img, float focal_length, float baseline)
  {
    // baseline (T) is typically negative in ROS convention, use absolute value
    const float abs_baseline = std::abs(baseline);
    
    if (focal_length <= 0 || abs_baseline <= 0) {
      RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
        "Invalid camera params: f=%.2f, T=%.4f - skipping depth clamp", 
        focal_length, baseline);
      return 0;
    }
    
    // Compute disparity thresholds from depth limits
    // disparity = f * |T| / depth
    // Close objects = high disparity, far objects = low disparity
    
    // Max disparity threshold (from min depth - objects too close)
    float max_disparity_threshold = std::numeric_limits<float>::max();
    if (min_depth_meters_ > 0) {
      max_disparity_threshold = focal_length * abs_baseline / static_cast<float>(min_depth_meters_);
    }
    
    // Min disparity threshold (from max depth - objects too far)
    float min_disparity_threshold = 0.0f;
    if (max_depth_meters_ > 0) {
      min_disparity_threshold = focal_length * abs_baseline / static_cast<float>(max_depth_meters_);
    }
    
    RCLCPP_DEBUG(this->get_logger(),
      "Depth clamp: min_depth=%.2fm -> max_disp=%.1f, max_depth=%.2fm -> min_disp=%.1f",
      min_depth_meters_, max_disparity_threshold, max_depth_meters_, min_disparity_threshold);
    
    int clamped = 0;
    const int rows = img.rows;
    const int cols = img.cols;
    
    for (int y = 0; y < rows; ++y) {
      float* row = img.ptr<float>(y);
      for (int x = 0; x < cols; ++x) {
        float d = row[x];
        
        // Only operate on valid pixels
        if (d <= 0) continue;
        
        // Invalidate if outside depth range
        if (d > max_disparity_threshold || d < min_disparity_threshold) {
          row[x] = 0.0f;  // Invalidate
          ++clamped;
        }
      }
    }
    
    return clamped;
  }
  
  // Domain Transform inspired 1D recursive filter pass
  void domainTransformPass(cv::Mat& img, bool horizontal)
  {
    const int rows = img.rows;
    const int cols = img.cols;
    const float alpha = static_cast<float>(smooth_alpha_);
    const float delta = static_cast<float>(smooth_delta_);
    
    if (horizontal) {
      // Horizontal pass
      for (int y = 0; y < rows; ++y) {
        float* row = img.ptr<float>(y);
        
        // Forward pass (left to right)
        for (int x = 1; x < cols; ++x) {
          float curr = row[x];
          float prev = row[x - 1];
          
          // Skip invalid pixels (disparity = 0 typically means invalid)
          if (curr <= 0 || prev <= 0) continue;
          
          // Compute edge-aware weight based on disparity difference
          float diff = std::abs(curr - prev);
          float weight = computeWeight(diff, delta, alpha);
          
          // Blend with neighbor
          row[x] = curr * (1.0f - weight) + prev * weight;
        }
        
        // Backward pass (right to left)
        for (int x = cols - 2; x >= 0; --x) {
          float curr = row[x];
          float next = row[x + 1];
          
          if (curr <= 0 || next <= 0) continue;
          
          float diff = std::abs(curr - next);
          float weight = computeWeight(diff, delta, alpha);
          
          row[x] = curr * (1.0f - weight) + next * weight;
        }
      }
    } else {
      // Vertical pass
      for (int x = 0; x < cols; ++x) {
        // Forward pass (top to bottom)
        for (int y = 1; y < rows; ++y) {
          float curr = img.at<float>(y, x);
          float prev = img.at<float>(y - 1, x);
          
          if (curr <= 0 || prev <= 0) continue;
          
          float diff = std::abs(curr - prev);
          float weight = computeWeight(diff, delta, alpha);
          
          img.at<float>(y, x) = curr * (1.0f - weight) + prev * weight;
        }
        
        // Backward pass (bottom to top)
        for (int y = rows - 2; y >= 0; --y) {
          float curr = img.at<float>(y, x);
          float next = img.at<float>(y + 1, x);
          
          if (curr <= 0 || next <= 0) continue;
          
          float diff = std::abs(curr - next);
          float weight = computeWeight(diff, delta, alpha);
          
          img.at<float>(y, x) = curr * (1.0f - weight) + next * weight;
        }
      }
    }
  }
  
  // Compute edge-aware blending weight
  // When diff > delta, weight approaches 0 (preserve edge)
  // When diff < delta, weight approaches alpha (smooth)
  inline float computeWeight(float diff, float delta, float alpha) const
  {
    // Exponential falloff based on edge strength
    // weight = alpha * exp(-diff / delta)
    return alpha * std::exp(-diff / delta);
  }
  
  // Symmetric hole filling - fills invalid (zero) pixels from valid neighbors
  void fillHoles(cv::Mat& img)
  {
    const int rows = img.rows;
    const int cols = img.cols;
    const int max_radius = (hole_fill_radius_ >= 9999) ? cols : hole_fill_radius_;
    
    for (int y = 0; y < rows; ++y) {
      float* row = img.ptr<float>(y);
      
      for (int x = 0; x < cols; ++x) {
        // Only fill holes (invalid pixels with disparity <= 0)
        if (row[x] > 0) continue;
        
        // Search symmetrically for valid neighbors
        float left_val = 0, right_val = 0;
        int left_dist = max_radius + 1, right_dist = max_radius + 1;
        
        // Search left
        for (int d = 1; d <= max_radius && x - d >= 0; ++d) {
          if (row[x - d] > 0) {
            left_val = row[x - d];
            left_dist = d;
            break;
          }
        }
        
        // Search right
        for (int d = 1; d <= max_radius && x + d < cols; ++d) {
          if (row[x + d] > 0) {
            right_val = row[x + d];
            right_dist = d;
            break;
          }
        }
        
        // Fill with interpolated or nearest value
        if (left_dist <= max_radius && right_dist <= max_radius) {
          // Both neighbors found - use distance-weighted average
          float total_dist = static_cast<float>(left_dist + right_dist);
          row[x] = (left_val * right_dist + right_val * left_dist) / total_dist;
        } else if (left_dist <= max_radius) {
          row[x] = left_val;
        } else if (right_dist <= max_radius) {
          row[x] = right_val;
        }
        // If neither found within radius, leave as invalid
      }
    }
  }

  rclcpp::Publisher<stereo_msgs::msg::DisparityImage>::SharedPtr pub_;
  rclcpp::Subscription<stereo_msgs::msg::DisparityImage>::SharedPtr sub_;
  
  int filter_magnitude_;
  double smooth_alpha_;
  int smooth_delta_;
  int hole_filling_;
  int hole_fill_radius_;
  double min_depth_meters_;
  double max_depth_meters_;
  int edge_invalidation_width_;
  double edge_canny_low_;
  double edge_canny_high_;
  int speckle_size_;
  double speckle_diff_;
};

}  // namespace maurice_cam

RCLCPP_COMPONENTS_REGISTER_NODE(maurice_cam::DisparityEdgePreservingFilter)
