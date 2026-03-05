/*
 * Copyright (c) 2026, Maurice Robotics
 * All rights reserved.
 *
 * DynamicGoalChecker - A configurable goal checker plugin for Nav2
 */

#include <memory>
#include <string>
#include <limits>
#include <vector>
#include <cmath>

#include "maurice_nav/plugins/dynamic_goal_checker.hpp"
#include "pluginlib/class_list_macros.hpp"
#include "angles/angles.h"
#include "nav2_util/node_utils.hpp"
#include "nav2_util/geometry_utils.hpp"

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wpedantic"
#include "tf2/utils.hpp"
#pragma GCC diagnostic pop

using rcl_interfaces::msg::ParameterType;
using std::placeholders::_1;

namespace maurice_nav
{

DynamicGoalChecker::DynamicGoalChecker()
: current_tier_(0)
{
}

void DynamicGoalChecker::initialize(
  const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent,
  const std::string & plugin_name,
  const std::shared_ptr<nav2_costmap_2d::Costmap2DROS> /*costmap_ros*/)
{
  plugin_name_ = plugin_name;
  auto node = parent.lock();

  if (!node) {
    throw std::runtime_error("Failed to lock parent node");
  }

  logger_ = node->get_logger();
  clock_ = node->get_clock();

  // Default arrays: single tier - 0.25m/0.25rad for 0 seconds (instant)
  // Velocity defaults to infinity (no velocity check)
  std::vector<double> default_times = {0.0};
  std::vector<double> default_xy = {0.25};
  std::vector<double> default_yaw = {0.25};
  std::vector<double> default_linear_vel = {std::numeric_limits<double>::infinity()};
  std::vector<double> default_angular_vel = {std::numeric_limits<double>::infinity()};

  // Declare parameters with defaults
  nav2_util::declare_parameter_if_not_declared(
    node,
    plugin_name + ".time_thresholds", rclcpp::ParameterValue(default_times));
  nav2_util::declare_parameter_if_not_declared(
    node,
    plugin_name + ".xy_tolerances", rclcpp::ParameterValue(default_xy));
  nav2_util::declare_parameter_if_not_declared(
    node,
    plugin_name + ".yaw_tolerances", rclcpp::ParameterValue(default_yaw));
  nav2_util::declare_parameter_if_not_declared(
    node,
    plugin_name + ".linear_vel_tolerances", rclcpp::ParameterValue(default_linear_vel));
  nav2_util::declare_parameter_if_not_declared(
    node,
    plugin_name + ".angular_vel_tolerances", rclcpp::ParameterValue(default_angular_vel));

  // Get parameter values
  node->get_parameter(plugin_name + ".time_thresholds", time_thresholds_);
  node->get_parameter(plugin_name + ".xy_tolerances", xy_tolerances_);
  node->get_parameter(plugin_name + ".yaw_tolerances", yaw_tolerances_);
  node->get_parameter(plugin_name + ".linear_vel_tolerances", linear_vel_tolerances_);
  node->get_parameter(plugin_name + ".angular_vel_tolerances", angular_vel_tolerances_);

  // Validate array sizes match
  size_t num_tiers = time_thresholds_.size();
  if (xy_tolerances_.size() != num_tiers ||
      yaw_tolerances_.size() != num_tiers ||
      linear_vel_tolerances_.size() != num_tiers ||
      angular_vel_tolerances_.size() != num_tiers)
  {
    RCLCPP_ERROR(
      logger_,
      "DynamicGoalChecker: all tolerance arrays must have same size (time=%zu, xy=%zu, yaw=%zu, lin_vel=%zu, ang_vel=%zu)",
      num_tiers, xy_tolerances_.size(), yaw_tolerances_.size(),
      linear_vel_tolerances_.size(), angular_vel_tolerances_.size());
    throw std::runtime_error("Parameter array size mismatch");
  }

  if (num_tiers == 0) {
    RCLCPP_ERROR(logger_, "DynamicGoalChecker: parameter arrays cannot be empty");
    throw std::runtime_error("Parameter arrays empty");
  }

  // Initialize tier tracking
  tier_start_times_.resize(time_thresholds_.size());
  tier_active_.resize(time_thresholds_.size(), false);
  current_tier_ = 0;

  RCLCPP_INFO(
    logger_,
    "DynamicGoalChecker initialized with %zu tolerance tiers",
    time_thresholds_.size());
  for (size_t i = 0; i < time_thresholds_.size(); ++i) {
    RCLCPP_INFO(
      logger_,
      "  Tier %zu: xy=%.3fm, yaw=%.3frad, lin_vel=%.3fm/s, ang_vel=%.3frad/s, time=%.2fs",
      i, xy_tolerances_[i], yaw_tolerances_[i],
      linear_vel_tolerances_[i], angular_vel_tolerances_[i], time_thresholds_[i]);
  }

  // Add callback for dynamic parameters
  dyn_params_handler_ = node->add_on_set_parameters_callback(
    std::bind(&DynamicGoalChecker::dynamicParametersCallback, this, _1));
}

void DynamicGoalChecker::reset()
{
  // Reset all tier timers
  for (size_t i = 0; i < tier_active_.size(); ++i) {
    tier_active_[i] = false;
  }
  current_tier_ = 0;
}

bool DynamicGoalChecker::isGoalReached(
  const geometry_msgs::msg::Pose & query_pose,
  const geometry_msgs::msg::Pose & goal_pose,
  const geometry_msgs::msg::Twist & velocity)
{
  // Calculate current distance and yaw error
  double dx = query_pose.position.x - goal_pose.position.x;
  double dy = query_pose.position.y - goal_pose.position.y;
  double dist_sq = dx * dx + dy * dy;

  double dyaw = std::fabs(angles::shortest_angular_distance(
    tf2::getYaw(query_pose.orientation),
    tf2::getYaw(goal_pose.orientation)));

  // Calculate current velocities
  double linear_vel = std::hypot(velocity.linear.x, velocity.linear.y);
  double angular_vel = std::fabs(velocity.angular.z);

  rclcpp::Time now = clock_->now();

  // Check each tolerance tier
  for (size_t i = 0; i < time_thresholds_.size(); ++i) {
    double xy_tol = xy_tolerances_[i];
    double yaw_tol = yaw_tolerances_[i];
    double lin_vel_tol = linear_vel_tolerances_[i];
    double ang_vel_tol = angular_vel_tolerances_[i];
    double time_thresh = time_thresholds_[i];

    bool in_tolerance = (dist_sq <= xy_tol * xy_tol) &&
                        (dyaw <= yaw_tol) &&
                        (linear_vel <= lin_vel_tol) &&
                        (angular_vel <= ang_vel_tol);

    if (in_tolerance) {
      if (!tier_active_[i]) {
        // Just entered this tolerance zone, start timer
        tier_start_times_[i] = now;
        tier_active_[i] = true;
      }

      // Check if we've been in this zone long enough
      double elapsed = (now - tier_start_times_[i]).seconds();
      if (elapsed >= time_thresh) {
        current_tier_ = i;
        RCLCPP_DEBUG(
          logger_,
          "Goal reached via tier %zu (xy=%.3f, yaw=%.3f, time=%.2fs)",
          i, xy_tol, yaw_tol, elapsed);
        return true;
      }
    } else {
      // Left this tolerance zone, reset timer
      tier_active_[i] = false;
    }
  }

  return false;
}

bool DynamicGoalChecker::getTolerances(
  geometry_msgs::msg::Pose & pose_tolerance,
  geometry_msgs::msg::Twist & vel_tolerance)
{
  double invalid_field = std::numeric_limits<double>::lowest();

  // Report the first tolerance tier
  double xy_tol = xy_tolerances_.empty() ? 0.25 : xy_tolerances_[0];
  double yaw_tol = yaw_tolerances_.empty() ? 0.25 : yaw_tolerances_[0];
  double lin_vel_tol = linear_vel_tolerances_.empty() ?
    std::numeric_limits<double>::infinity() : linear_vel_tolerances_[0];
  double ang_vel_tol = angular_vel_tolerances_.empty() ?
    std::numeric_limits<double>::infinity() : angular_vel_tolerances_[0];

  pose_tolerance.position.x = xy_tol;
  pose_tolerance.position.y = xy_tol;
  pose_tolerance.position.z = invalid_field;
  pose_tolerance.orientation =
    nav2_util::geometry_utils::orientationAroundZAxis(yaw_tol);

  // Report velocity tolerances (use invalid_field if infinity)
  vel_tolerance.linear.x = std::isinf(lin_vel_tol) ? invalid_field : lin_vel_tol;
  vel_tolerance.linear.y = std::isinf(lin_vel_tol) ? invalid_field : lin_vel_tol;
  vel_tolerance.linear.z = invalid_field;

  vel_tolerance.angular.x = invalid_field;
  vel_tolerance.angular.y = invalid_field;
  vel_tolerance.angular.z = std::isinf(ang_vel_tol) ? invalid_field : ang_vel_tol;

  return true;
}

rcl_interfaces::msg::SetParametersResult
DynamicGoalChecker::dynamicParametersCallback(std::vector<rclcpp::Parameter> parameters)
{
  rcl_interfaces::msg::SetParametersResult result;
  result.successful = true;

  // Temporary storage for validation
  std::vector<double> new_time_thresholds = time_thresholds_;
  std::vector<double> new_xy_tolerances = xy_tolerances_;
  std::vector<double> new_yaw_tolerances = yaw_tolerances_;
  std::vector<double> new_linear_vel_tolerances = linear_vel_tolerances_;
  std::vector<double> new_angular_vel_tolerances = angular_vel_tolerances_;
  bool arrays_changed = false;

  for (auto & parameter : parameters) {
    const auto & param_type = parameter.get_type();
    const auto & param_name = parameter.get_name();

    // Only process parameters for this plugin
    if (param_name.find(plugin_name_ + ".") != 0) {
      continue;
    }

    if (param_type == ParameterType::PARAMETER_DOUBLE_ARRAY) {
      if (param_name == plugin_name_ + ".time_thresholds") {
        new_time_thresholds = parameter.as_double_array();
        arrays_changed = true;
      } else if (param_name == plugin_name_ + ".xy_tolerances") {
        new_xy_tolerances = parameter.as_double_array();
        arrays_changed = true;
      } else if (param_name == plugin_name_ + ".yaw_tolerances") {
        new_yaw_tolerances = parameter.as_double_array();
        arrays_changed = true;
      } else if (param_name == plugin_name_ + ".linear_vel_tolerances") {
        new_linear_vel_tolerances = parameter.as_double_array();
        arrays_changed = true;
      } else if (param_name == plugin_name_ + ".angular_vel_tolerances") {
        new_angular_vel_tolerances = parameter.as_double_array();
        arrays_changed = true;
      }
    }
  }

  // Validate and apply array changes
  if (arrays_changed) {
    size_t num_tiers = new_time_thresholds.size();
    if (new_xy_tolerances.size() != num_tiers ||
        new_yaw_tolerances.size() != num_tiers ||
        new_linear_vel_tolerances.size() != num_tiers ||
        new_angular_vel_tolerances.size() != num_tiers)
    {
      RCLCPP_ERROR(
        logger_,
        "Parameter array size mismatch: time=%zu, xy=%zu, yaw=%zu, lin_vel=%zu, ang_vel=%zu",
        num_tiers, new_xy_tolerances.size(), new_yaw_tolerances.size(),
        new_linear_vel_tolerances.size(), new_angular_vel_tolerances.size());
      result.successful = false;
      result.reason = "Array sizes must match";
      return result;
    }

    if (num_tiers == 0) {
      RCLCPP_ERROR(logger_, "Parameter arrays cannot be empty");
      result.successful = false;
      result.reason = "Arrays cannot be empty";
      return result;
    }

    time_thresholds_ = new_time_thresholds;
    xy_tolerances_ = new_xy_tolerances;
    yaw_tolerances_ = new_yaw_tolerances;
    linear_vel_tolerances_ = new_linear_vel_tolerances;
    angular_vel_tolerances_ = new_angular_vel_tolerances;

    // Resize tier tracking arrays
    tier_start_times_.resize(time_thresholds_.size());
    tier_active_.resize(time_thresholds_.size(), false);

    RCLCPP_INFO(logger_, "Updated tolerance tiers to %zu tiers", time_thresholds_.size());
  }

  return result;
}

}  // namespace maurice_nav

// Register the plugin with pluginlib
PLUGINLIB_EXPORT_CLASS(maurice_nav::DynamicGoalChecker, nav2_core::GoalChecker)
