// Filter chain initialisation, logging, and orchestration.
// Individual filter implementations live in sibling files.

#include "maurice_cam/stereo_depth_estimator.hpp"

#include <algorithm>
#include <chrono>
#include <string>

namespace maurice_cam
{

// =============================================================================
// Parameter initialisation
// =============================================================================
void StereoDepthEstimator::initFilterParams()
{
  // 0. Downsample — run all filters at reduced resolution
  this->declare_parameter<bool>("filter_downsample.enabled", true);
  this->declare_parameter<int>("filter_downsample.factor", 4);
  filter_downsample_enabled_ = this->get_parameter("filter_downsample.enabled").as_bool();
  filter_downsample_factor_ = std::clamp(
      static_cast<int>(this->get_parameter("filter_downsample.factor").as_int()), 1, 8);

  // 1. Median
  this->declare_parameter<bool>("median.enabled", false);
  this->declare_parameter<int>("median.kernel_size", 5);
  median_enabled_ = this->get_parameter("median.enabled").as_bool();
  median_kernel_size_ = static_cast<int>(this->get_parameter("median.kernel_size").as_int());
  if (median_kernel_size_ % 2 == 0) median_kernel_size_ += 1;
  median_kernel_size_ = std::max(3, median_kernel_size_);

  // 2. Bilateral
  this->declare_parameter<bool>("bilateral.enabled", false);
  this->declare_parameter<int>("bilateral.diameter", 5);
  this->declare_parameter<double>("bilateral.sigma_color", 10.0);
  this->declare_parameter<double>("bilateral.sigma_space", 10.0);
  bilateral_enabled_ = this->get_parameter("bilateral.enabled").as_bool();
  bilateral_diameter_ = static_cast<int>(this->get_parameter("bilateral.diameter").as_int());
  bilateral_sigma_color_ = this->get_parameter("bilateral.sigma_color").as_double();
  bilateral_sigma_space_ = this->get_parameter("bilateral.sigma_space").as_double();

  // 3. Hole Filling
  this->declare_parameter<bool>("hole_filling.enabled", false);
  this->declare_parameter<int>("hole_filling.mode", 1);
  this->declare_parameter<int>("hole_filling.strategy", 1);
  hole_filling_enabled_ = this->get_parameter("hole_filling.enabled").as_bool();
  int hf_mode = std::clamp(static_cast<int>(this->get_parameter("hole_filling.mode").as_int()), 0, 5);
  const int hf_map[] = {0, 2, 4, 8, 16, 9999};
  hole_fill_radius_ = hf_map[hf_mode];
  hole_fill_strategy_ = std::clamp(static_cast<int>(this->get_parameter("hole_filling.strategy").as_int()), 0, 2);

  // 4. Depth Clamping
  this->declare_parameter<bool>("depth_clamp.enabled", false);
  this->declare_parameter<double>("depth_clamp.min_depth_meters", 0.25);
  this->declare_parameter<double>("depth_clamp.max_depth_meters", 5.0);
  depth_clamp_enabled_ = this->get_parameter("depth_clamp.enabled").as_bool();
  min_depth_meters_ = this->get_parameter("depth_clamp.min_depth_meters").as_double();
  max_depth_meters_ = this->get_parameter("depth_clamp.max_depth_meters").as_double();

  // 5. Edge Invalidation
  this->declare_parameter<bool>("edge_invalidation.enabled", false);
  this->declare_parameter<int>("edge_invalidation.width", 3);
  this->declare_parameter<double>("edge_invalidation.canny_low", 10.0);
  this->declare_parameter<double>("edge_invalidation.canny_high", 30.0);
  edge_inv_enabled_ = this->get_parameter("edge_invalidation.enabled").as_bool();
  edge_inv_width_ = std::max(1, static_cast<int>(this->get_parameter("edge_invalidation.width").as_int()));
  edge_canny_low_ = this->get_parameter("edge_invalidation.canny_low").as_double();
  edge_canny_high_ = this->get_parameter("edge_invalidation.canny_high").as_double();

  // 6. Speckle
  this->declare_parameter<bool>("speckle.enabled", false);
  this->declare_parameter<int>("speckle.max_size", 200);
  this->declare_parameter<double>("speckle.max_diff", 1.0);
  speckle_enabled_ = this->get_parameter("speckle.enabled").as_bool();
  speckle_max_size_ = std::max(1, static_cast<int>(this->get_parameter("speckle.max_size").as_int()));
  speckle_max_diff_ = std::max(0.0, this->get_parameter("speckle.max_diff").as_double());

  // 7. Domain Transform
  this->declare_parameter<bool>("domain_transform.enabled", false);
  this->declare_parameter<int>("domain_transform.iterations", 2);
  this->declare_parameter<double>("domain_transform.alpha", 0.5);
  this->declare_parameter<int>("domain_transform.delta", 20);
  dt_enabled_ = this->get_parameter("domain_transform.enabled").as_bool();
  dt_iterations_ = std::clamp(static_cast<int>(this->get_parameter("domain_transform.iterations").as_int()), 1, 5);
  dt_alpha_ = std::clamp(this->get_parameter("domain_transform.alpha").as_double(), 0.25, 1.0);
  dt_delta_ = std::clamp(static_cast<int>(this->get_parameter("domain_transform.delta").as_int()), 1, 50);

  // 8. Temporal
  this->declare_parameter<bool>("temporal.enabled", false);
  this->declare_parameter<double>("temporal.alpha", 0.4);
  this->declare_parameter<int>("temporal.delta", 20);
  this->declare_parameter<int>("temporal.persistence", 3);
  temporal_enabled_ = this->get_parameter("temporal.enabled").as_bool();
  temporal_alpha_ = std::clamp(this->get_parameter("temporal.alpha").as_double(), 0.0, 1.0);
  temporal_delta_ = std::clamp(static_cast<int>(this->get_parameter("temporal.delta").as_int()), 1, 100);
  temporal_persistence_ = std::clamp(static_cast<int>(this->get_parameter("temporal.persistence").as_int()), 0, 8);

  // Filter execution order
  this->declare_parameter<std::vector<std::string>>("filter_order", {
    "median", "bilateral", "hole_filling", "depth_clamp",
    "edge_invalidation", "speckle", "domain_transform", "temporal"});
  filter_order_ = this->get_parameter("filter_order").as_string_array();
}

// =============================================================================
// Logging
// =============================================================================
void StereoDepthEstimator::logFilterConfig() const
{
  auto log = [this](const char* name, bool on, const std::string & detail = "") {
    RCLCPP_INFO(this->get_logger(), "  %-22s %s%s", name, on ? "ON" : "OFF", detail.c_str());
  };

  RCLCPP_INFO(this->get_logger(), "=== Disparity Filter Chain ===");
  log("Downsample", filter_downsample_enabled_,
      filter_downsample_enabled_ ? "  factor=" + std::to_string(filter_downsample_factor_) : "");

  // Print execution order
  std::string order_str;
  for (const auto& name : filter_order_) {
    if (!order_str.empty()) order_str += " → ";
    order_str += name;
  }
  RCLCPP_INFO(this->get_logger(), "  Order: %s", order_str.c_str());

  log("Median", median_enabled_,
      median_enabled_ ? "  kernel=" + std::to_string(median_kernel_size_) : "");
  log("Bilateral", bilateral_enabled_,
      bilateral_enabled_ ? "  d=" + std::to_string(bilateral_diameter_) : "");
  log("Hole Filling", hole_filling_enabled_,
      hole_filling_enabled_ ? "  radius=" + std::to_string(hole_fill_radius_)
        + " strategy=" + std::to_string(hole_fill_strategy_) : "");
  log("Depth Clamping", depth_clamp_enabled_,
      depth_clamp_enabled_ ? "  min=" + std::to_string(min_depth_meters_)
        + "m max=" + std::to_string(max_depth_meters_) + "m" : "");
  log("Edge Invalidation", edge_inv_enabled_,
      edge_inv_enabled_ ? "  width=" + std::to_string(edge_inv_width_) : "");
  log("Speckle Removal", speckle_enabled_,
      speckle_enabled_ ? "  size=" + std::to_string(speckle_max_size_) : "");
  log("Domain Transform", dt_enabled_,
      dt_enabled_ ? "  iter=" + std::to_string(dt_iterations_)
        + " a=" + std::to_string(dt_alpha_)
        + " d=" + std::to_string(dt_delta_) : "");
  log("Temporal", temporal_enabled_,
      temporal_enabled_ ? "  alpha=" + std::to_string(temporal_alpha_)
        + " delta=" + std::to_string(temporal_delta_)
        + " persist=" + std::to_string(temporal_persistence_) : "");
}

// =============================================================================
// Filter chain orchestration
// =============================================================================
void StereoDepthEstimator::applyFilterChain(cv::Mat& disparity, cv::Mat& disparity_lowres,
                                        FilterTimings& timings, float focal_length, float baseline)
{
  using clock = std::chrono::steady_clock;
  const cv::Size orig_size = disparity.size();
  const int f = filter_downsample_factor_;

  // Downsample for faster filtering (INTER_AREA averages the pixel block → free noise reduction)
  auto t0 = clock::now();
  if (filter_downsample_enabled_ && f > 1) {
    cv::resize(disparity, disparity,
               cv::Size(orig_size.width / f, orig_size.height / f),
               0, 0, cv::INTER_AREA);
  }
  auto t1 = clock::now();
  timings.downsample_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();

  // Run filters in configured order
  for (const auto& name : filter_order_) {
    t0 = clock::now();
    if (name == "median" && median_enabled_) {
      applyMedian(disparity);
      t1 = clock::now();
      timings.median_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "bilateral" && bilateral_enabled_) {
      applyBilateral(disparity);
      t1 = clock::now();
      timings.bilateral_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "hole_filling" && hole_filling_enabled_) {
      fillHoles(disparity);
      t1 = clock::now();
      timings.hole_fill_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "depth_clamp" && depth_clamp_enabled_) {
      clampByDepth(disparity, focal_length, baseline);
      t1 = clock::now();
      timings.depth_clamp_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "edge_invalidation" && edge_inv_enabled_) {
      invalidateEdges(disparity);
      t1 = clock::now();
      timings.edge_inv_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "speckle" && speckle_enabled_) {
      filterSpeckles(disparity);
      t1 = clock::now();
      timings.speckle_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "domain_transform" && dt_enabled_) {
      applyDomainTransform(disparity);
      t1 = clock::now();
      timings.domain_transform_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    } else if (name == "temporal" && temporal_enabled_) {
      applyTemporal(disparity);
      t1 = clock::now();
      timings.temporal_ms += std::chrono::duration<double, std::milli>(t1 - t0).count();
    }
  }

  // Save low-res filtered result for point cloud before upsampling
  disparity_lowres = disparity.clone();

  // Upsample back to original resolution for disparity publishing / depth map
  t0 = clock::now();
  if (filter_downsample_enabled_ && f > 1) {
    cv::resize(disparity, disparity, orig_size, 0, 0, cv::INTER_LINEAR);
  }
  t1 = clock::now();
  timings.upsample_ms = std::chrono::duration<double, std::milli>(t1 - t0).count();
}

} // namespace maurice_cam
