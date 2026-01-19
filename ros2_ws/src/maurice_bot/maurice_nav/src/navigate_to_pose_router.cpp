#include "maurice_nav/navigate_to_pose_router.hpp"

#include <functional>
#include <memory>
#include <string>

namespace maurice_nav
{

NavigateToPoseRouter::NavigateToPoseRouter(const rclcpp::NodeOptions& options)
: Node("navigate_to_pose_router", options),
  current_mode_("mapfree")  // Default mode
{
  // Use separate mutually exclusive callback groups so server can call client
  server_callback_group_ = create_callback_group(
    rclcpp::CallbackGroupType::MutuallyExclusive);
  client_callback_group_ = create_callback_group(
    rclcpp::CallbackGroupType::MutuallyExclusive);

  // QoS profile for persistent/latched topic
  auto latched_qos = rclcpp::QoS(1)
    .durability(RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL)
    .reliability(RMW_QOS_POLICY_RELIABILITY_RELIABLE);

  // Subscribe to current navigation mode
  mode_sub_ = create_subscription<std_msgs::msg::String>(
    "/nav/current_mode",
    latched_qos,
    std::bind(&NavigateToPoseRouter::modeCallback, this, std::placeholders::_1));

  // Publishers for current planner and goal checker selection
  current_planner_pub_ = create_publisher<std_msgs::msg::String>(
    "/nav/current_planner", latched_qos);

  current_goal_checker_pub_ = create_publisher<std_msgs::msg::String>(
    "/nav/current_goal_checker", latched_qos);

  // Publish initial values at startup (mapfree mode)
  auto initial_planner = std_msgs::msg::String();
  initial_planner.data = "mapfree";
  current_planner_pub_->publish(initial_planner);

  auto initial_goal_checker = std_msgs::msg::String();
  initial_goal_checker.data = "goal_checker_precise";
  current_goal_checker_pub_->publish(initial_goal_checker);

  RCLCPP_INFO(get_logger(), "Published initial planner: mapfree, goal_checker: goal_checker_precise");

  // Create action client to forward requests to internal action
  action_client_ = rclcpp_action::create_client<NavigateToPose>(
    this,
    "/internal_navigate_to_pose",
    client_callback_group_);

  // Create action server to receive requests
  action_server_ = rclcpp_action::create_server<NavigateToPose>(
    this,
    "/navigate_to_pose",
    std::bind(&NavigateToPoseRouter::goalCallback, this, std::placeholders::_1, std::placeholders::_2),
    std::bind(&NavigateToPoseRouter::cancelCallback, this, std::placeholders::_1),
    std::bind(&NavigateToPoseRouter::executeCallback, this, std::placeholders::_1),
    rcl_action_server_get_default_options(),
    server_callback_group_);

  RCLCPP_INFO(get_logger(), "NavigateToPoseRouter initialized");
  RCLCPP_INFO(get_logger(), "  Listening on: /navigate_to_pose");
  RCLCPP_INFO(get_logger(), "  Forwarding to: /internal_navigate_to_pose");
}

void NavigateToPoseRouter::modeCallback(const std_msgs::msg::String::SharedPtr msg)
{
  current_mode_ = msg->data;
  RCLCPP_DEBUG(get_logger(), "Current mode updated: %s", current_mode_.c_str());
}

rclcpp_action::GoalResponse NavigateToPoseRouter::goalCallback(
  const rclcpp_action::GoalUUID& /* uuid */,
  std::shared_ptr<const NavigateToPose::Goal> /* goal */)
{
  RCLCPP_INFO(get_logger(), "Received navigate_to_pose goal request");
  return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
}

rclcpp_action::CancelResponse NavigateToPoseRouter::cancelCallback(
  const std::shared_ptr<GoalHandleNavigateToPose> goal_handle)
{
  RCLCPP_INFO(get_logger(), "Received cancel request");

  // Try to cancel the corresponding internal goal
  std::string goal_id_str = rclcpp_action::to_string(goal_handle->get_goal_id());

  auto it = goal_handle_map_.find(goal_id_str);
  if (it != goal_handle_map_.end()) {
    auto client_goal_handle = it->second;
    if (client_goal_handle != nullptr) {
      RCLCPP_INFO(get_logger(), "Forwarding cancel to internal action");
      auto cancel_future = action_client_->async_cancel_goal(client_goal_handle);
    }
  }

  return rclcpp_action::CancelResponse::ACCEPT;
}

void NavigateToPoseRouter::executeCallback(
  const std::shared_ptr<GoalHandleNavigateToPose> goal_handle)
{
  RCLCPP_INFO(get_logger(), "Executing navigate_to_pose goal...");

  // Wait for the internal action server to be available
  if (!action_client_->wait_for_action_server(std::chrono::seconds(5))) {
    RCLCPP_ERROR(get_logger(), "Internal navigate_to_pose action server not available!");
    auto result = std::make_shared<NavigateToPose::Result>();
    goal_handle->abort(result);
    return;
  }

  // Create the goal to forward
  auto goal_msg = NavigateToPose::Goal();
  goal_msg.pose = goal_handle->get_goal()->pose;
  // Leave behavior_tree empty

  // Determine the frame to use
  std::string requested_frame = goal_handle->get_goal()->behavior_tree;
  if (requested_frame.empty()) {
    // If no frame specified, use the current mode
    requested_frame = current_mode_;
  }

  // If in mapping mode, reject the navigation request
  if (requested_frame == "mapping") {
    RCLCPP_WARN(get_logger(), "Navigation rejected: currently in mapping mode");
    auto result = std::make_shared<NavigateToPose::Result>();
    goal_handle->abort(result);
    return;
  }

  // Determine planner and goal checker based on frame
  std::string planner, goal_checker;
  if (requested_frame == "navigation") {
    planner = "navigation";
    goal_checker = "goal_checker";
  } else {  // mapfree or any other
    planner = "mapfree";
    goal_checker = "goal_checker_precise";
  }

  // Publish the planner selection
  auto planner_msg = std_msgs::msg::String();
  planner_msg.data = planner;
  current_planner_pub_->publish(planner_msg);

  // Publish the goal checker selection
  auto goal_checker_msg = std_msgs::msg::String();
  goal_checker_msg.data = goal_checker;
  current_goal_checker_pub_->publish(goal_checker_msg);

  RCLCPP_INFO(get_logger(), "Published planner: %s, goal_checker: %s",
    planner.c_str(), goal_checker.c_str());

  RCLCPP_INFO(get_logger(),
    "Forwarding goal to /nav/navigate_to_pose: position=(%.2f, %.2f, %.2f)",
    goal_msg.pose.pose.position.x,
    goal_msg.pose.pose.position.y,
    goal_msg.pose.pose.position.z);

  // Send goal to internal action server with feedback callback
  auto send_goal_options = rclcpp_action::Client<NavigateToPose>::SendGoalOptions();
  send_goal_options.feedback_callback =
    [this, goal_handle](
      typename ClientGoalHandle::SharedPtr client_goal_handle,
      const std::shared_ptr<const NavigateToPose::Feedback> feedback)
    {
      this->feedbackCallback(goal_handle, client_goal_handle, feedback);
    };

  auto send_goal_future = action_client_->async_send_goal(goal_msg, send_goal_options);

  // Wait for goal acceptance
  auto future_status = send_goal_future.wait_for(std::chrono::seconds(5));
  if (future_status != std::future_status::ready) {
    RCLCPP_ERROR(get_logger(), "Failed to send goal to internal action server");
    auto result = std::make_shared<NavigateToPose::Result>();
    goal_handle->abort(result);
    return;
  }

  auto client_goal_handle = send_goal_future.get();

  if (!client_goal_handle) {
    RCLCPP_WARN(get_logger(), "Internal goal was rejected");
    auto result = std::make_shared<NavigateToPose::Result>();
    goal_handle->abort(result);
    return;
  }

  RCLCPP_INFO(get_logger(), "Internal goal accepted");

  // Store mapping for cancel handling
  std::string goal_id_str = rclcpp_action::to_string(goal_handle->get_goal_id());
  goal_handle_map_[goal_id_str] = client_goal_handle;

  // Wait for the result
  auto result_future = action_client_->async_get_result(client_goal_handle);

  future_status = result_future.wait_for(std::chrono::seconds(0));
  while (rclcpp::ok() && future_status != std::future_status::ready) {
    // Check if the goal was canceled
    if (goal_handle->is_canceling()) {
      RCLCPP_INFO(get_logger(), "Goal canceled by client");
      auto result = std::make_shared<NavigateToPose::Result>();
      goal_handle->canceled(result);
      // Clean up goal handle mapping
      goal_handle_map_.erase(goal_id_str);
      return;
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    future_status = result_future.wait_for(std::chrono::seconds(0));
  }

  if (!rclcpp::ok()) {
    return;
  }

  auto result_response = result_future.get();

  // Forward the result status
  auto result = result_response.result;

  switch (result_response.code) {
    case rclcpp_action::ResultCode::SUCCEEDED:
      RCLCPP_INFO(get_logger(), "Internal goal succeeded");
      goal_handle->succeed(result);
      break;
    case rclcpp_action::ResultCode::CANCELED:
      RCLCPP_INFO(get_logger(), "Internal goal was canceled");
      goal_handle->canceled(result);
      break;
    case rclcpp_action::ResultCode::ABORTED:
      RCLCPP_WARN(get_logger(), "Internal goal was aborted");
      goal_handle->abort(result);
      break;
    default:
      RCLCPP_WARN(get_logger(), "Internal goal failed with unknown status");
      goal_handle->abort(result);
      break;
  }

  // Clean up goal handle mapping
  goal_handle_map_.erase(goal_id_str);
}

void NavigateToPoseRouter::feedbackCallback(
  std::shared_ptr<GoalHandleNavigateToPose> server_goal_handle,
  typename ClientGoalHandle::SharedPtr /* client_goal_handle */,
  const std::shared_ptr<const NavigateToPose::Feedback> feedback)
{
  RCLCPP_DEBUG(get_logger(), "Forwarding feedback");
  // Create a non-const copy for publishing
  auto feedback_copy = std::make_shared<NavigateToPose::Feedback>(*feedback);
  server_goal_handle->publish_feedback(feedback_copy);
}

}  // namespace maurice_nav
