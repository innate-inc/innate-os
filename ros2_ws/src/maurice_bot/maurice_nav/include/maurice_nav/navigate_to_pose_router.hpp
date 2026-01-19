#ifndef MAURICE_NAV__NAVIGATE_TO_POSE_ROUTER_HPP_
#define MAURICE_NAV__NAVIGATE_TO_POSE_ROUTER_HPP_

#include <string>
#include <memory>
#include <unordered_map>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"
#include "std_msgs/msg/string.hpp"
#include "nav2_msgs/action/navigate_to_pose.hpp"

namespace maurice_nav
{

/**
 * @brief NavigateToPose action router/proxy
 *
 * This node acts as a router for the NavigateToPose action.
 * It listens on /navigate_to_pose and forwards all requests to /internal_navigate_to_pose.
 * It also selects the appropriate planner and goal checker based on the current mode.
 */
class NavigateToPoseRouter : public rclcpp::Node
{
public:
  using NavigateToPose = nav2_msgs::action::NavigateToPose;
  using GoalHandleNavigateToPose = rclcpp_action::ServerGoalHandle<NavigateToPose>;
  using ClientGoalHandle = rclcpp_action::ClientGoalHandle<NavigateToPose>;

  /**
   * @brief Construct a new Navigate To Pose Router
   *
   * @param options Node options
   */
  explicit NavigateToPoseRouter(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());

private:
  /**
   * @brief Goal callback for action server
   */
  rclcpp_action::GoalResponse goalCallback(
    const rclcpp_action::GoalUUID& uuid,
    std::shared_ptr<const NavigateToPose::Goal> goal);

  /**
   * @brief Cancel callback for action server
   */
  rclcpp_action::CancelResponse cancelCallback(
    const std::shared_ptr<GoalHandleNavigateToPose> goal_handle);

  /**
   * @brief Execute callback for action server
   */
  void executeCallback(
    const std::shared_ptr<GoalHandleNavigateToPose> goal_handle);

  /**
   * @brief Feedback callback for action client
   */
  void feedbackCallback(
    std::shared_ptr<GoalHandleNavigateToPose> server_goal_handle,
    typename ClientGoalHandle::SharedPtr client_goal_handle,
    const std::shared_ptr<const NavigateToPose::Feedback> feedback);

  /**
   * @brief Mode subscription callback
   */
  void modeCallback(const std_msgs::msg::String::SharedPtr msg);

  // Action server
  rclcpp_action::Server<NavigateToPose>::SharedPtr action_server_;

  // Action client
  rclcpp_action::Client<NavigateToPose>::SharedPtr action_client_;

  // Publishers
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr current_planner_pub_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr current_goal_checker_pub_;

  // Subscribers
  rclcpp::Subscription<std_msgs::msg::String>::SharedPtr mode_sub_;

  // Callback groups
  rclcpp::CallbackGroup::SharedPtr server_callback_group_;
  rclcpp::CallbackGroup::SharedPtr client_callback_group_;

  // Track active goals for cancel forwarding
  std::unordered_map<std::string, typename ClientGoalHandle::SharedPtr> goal_handle_map_;

  // Track current navigation mode
  std::string current_mode_;
};

}  // namespace maurice_nav

#endif  // MAURICE_NAV__NAVIGATE_TO_POSE_ROUTER_HPP_
