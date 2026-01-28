/*
 * Copyright (c) 2026, Maurice Robotics
 * All rights reserved.
 *
 * DynamicGoalChecker - A configurable goal checker plugin for Nav2
 */

#ifndef MAURICE_NAV__PLUGINS__DYNAMIC_GOAL_CHECKER_HPP_
#define MAURICE_NAV__PLUGINS__DYNAMIC_GOAL_CHECKER_HPP_

#include <memory>
#include <string>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_lifecycle/lifecycle_node.hpp"
#include "nav2_core/goal_checker.hpp"
#include "rcl_interfaces/msg/set_parameters_result.hpp"

namespace maurice_nav
{

/**
 * @class DynamicGoalChecker
 * @brief A dynamic goal checker plugin for Nav2
 *
 * This goal checker allows for dynamic adjustment of goal tolerances
 * based on configurable parameters. Extend this class to implement
 * custom goal checking logic.
 */
class DynamicGoalChecker : public nav2_core::GoalChecker
{
public:
  DynamicGoalChecker();
  ~DynamicGoalChecker() override = default;

  /**
   * @brief Initialize the goal checker
   * @param parent Weak pointer to the parent lifecycle node
   * @param plugin_name Name of this plugin instance
   * @param costmap_ros Shared pointer to the costmap
   */
  void initialize(
    const rclcpp_lifecycle::LifecycleNode::WeakPtr & parent,
    const std::string & plugin_name,
    const std::shared_ptr<nav2_costmap_2d::Costmap2DROS> costmap_ros) override;

  /**
   * @brief Reset the goal checker state
   */
  void reset() override;

  /**
   * @brief Check if the goal has been reached
   * @param query_pose Current robot pose
   * @param goal_pose Target goal pose
   * @param velocity Current robot velocity
   * @return True if goal is reached, false otherwise
   */
  bool isGoalReached(
    const geometry_msgs::msg::Pose & query_pose,
    const geometry_msgs::msg::Pose & goal_pose,
    const geometry_msgs::msg::Twist & velocity) override;

  /**
   * @brief Get the current tolerances
   * @param pose_tolerance Output pose tolerance
   * @param vel_tolerance Output velocity tolerance
   * @return True if tolerances are valid
   */
  bool getTolerances(
    geometry_msgs::msg::Pose & pose_tolerance,
    geometry_msgs::msg::Twist & vel_tolerance) override;

protected:
  // Goal tolerance parameters (parallel arrays defining tolerance tiers)
  // Each tier: if robot stays within all tolerances for time_thresholds[i] seconds,
  // the goal is considered reached
  std::vector<double> time_thresholds_;       // Required duration in each tolerance zone (seconds)
  std::vector<double> xy_tolerances_;         // XY distance tolerance for each tier (meters)
  std::vector<double> yaw_tolerances_;        // Yaw tolerance for each tier (radians)
  std::vector<double> linear_vel_tolerances_; // Max linear velocity for each tier (m/s)
  std::vector<double> angular_vel_tolerances_;// Max angular velocity for each tier (rad/s)

  // Per-tier timing: tracks how long robot has been in each tolerance zone
  std::vector<rclcpp::Time> tier_start_times_;  // When robot entered each tier
  std::vector<bool> tier_active_;               // Whether robot is currently in each tier

  // Current active tier (for reporting tolerances)
  size_t current_tier_;

  // Plugin name for parameter namespacing
  std::string plugin_name_;

  // Clock for time tracking
  rclcpp::Clock::SharedPtr clock_;

  // Logger for this plugin
  rclcpp::Logger logger_{rclcpp::get_logger("DynamicGoalChecker")};

  // Dynamic parameters handler
  rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr dyn_params_handler_;

  /**
   * @brief Callback executed when a parameter change is detected
   * @param parameters List of changed parameters
   * @return Result of the parameter change
   */
  rcl_interfaces::msg::SetParametersResult
  dynamicParametersCallback(std::vector<rclcpp::Parameter> parameters);
};

}  // namespace maurice_nav

#endif  // MAURICE_NAV__PLUGINS__DYNAMIC_GOAL_CHECKER_HPP_
