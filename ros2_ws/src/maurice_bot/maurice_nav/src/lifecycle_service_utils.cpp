#include "maurice_nav/lifecycle_service_utils.hpp"

namespace maurice_nav
{

LifecycleServiceClient::LifecycleServiceClient(
  rclcpp::Node::SharedPtr node,
  rclcpp::CallbackGroup::SharedPtr callback_group)
: node_(node), callback_group_(callback_group)
{
}

std::optional<uint8_t> LifecycleServiceClient::getNodeState(
  const std::string& node_name,
  double timeout_sec)
{
  auto request = std::make_shared<lifecycle_msgs::srv::GetState::Request>();
  std::string service_name = "/" + node_name + "/get_state";

  auto response = callService<lifecycle_msgs::srv::GetState>(
    service_name, request, timeout_sec);

  if (response.has_value()) {
    return (*response)->current_state.id;
  }

  return std::nullopt;
}

bool LifecycleServiceClient::sendLifecycleTransition(
  const std::string& node_name,
  uint8_t transition_id,
  double timeout_sec)
{
  auto request = std::make_shared<lifecycle_msgs::srv::ChangeState::Request>();
  request->transition.id = transition_id;

  std::string service_name = "/" + node_name + "/change_state";

  auto response = callService<lifecycle_msgs::srv::ChangeState>(
    service_name, request, timeout_sec);

  return response.has_value() && (*response)->success;
}

bool LifecycleServiceClient::transitionNode(
  const std::string& node_name,
  uint8_t target_state,
  bool only_up)
{
  using lifecycle_msgs::msg::State;
  using lifecycle_msgs::msg::Transition;

  try {
    // Get current state
    auto current_state_opt = getNodeState(node_name);

    if (!current_state_opt.has_value()) {
      RCLCPP_WARN(node_->get_logger(), "Failed to get state for %s", node_name.c_str());
      return false;
    }

    uint8_t current_state = current_state_opt.value();

    // Already at target state
    if (current_state == target_state) {
      RCLCPP_DEBUG(
        node_->get_logger(),
        "%s already at target state %d",
        node_name.c_str(), target_state);
      return true;
    }

    // Transition down (ACTIVE -> INACTIVE -> UNCONFIGURED)
    if (current_state > target_state) {
      // If only_up is True, don't transition down - just return True
      if (only_up) {
        RCLCPP_DEBUG(
          node_->get_logger(),
          "%s at state %d, skipping downward transition (only_up=true)",
          node_name.c_str(), current_state);
        return true;
      }

      // Deactivate if needed
      if (current_state == State::PRIMARY_STATE_ACTIVE) {
        if (!sendLifecycleTransition(node_name, Transition::TRANSITION_DEACTIVATE)) {
          RCLCPP_WARN(node_->get_logger(), "Failed to deactivate %s", node_name.c_str());
          return false;
        }

        current_state = State::PRIMARY_STATE_INACTIVE;
        if (current_state == target_state) {
          return true;
        }
      }

      // Cleanup if needed
      if (current_state == State::PRIMARY_STATE_INACTIVE &&
        target_state == State::PRIMARY_STATE_UNCONFIGURED)
      {
        if (!sendLifecycleTransition(node_name, Transition::TRANSITION_CLEANUP)) {
          RCLCPP_WARN(node_->get_logger(), "Failed to cleanup %s", node_name.c_str());
          return false;
        }

        return true;
      }
    }
    // Transition up (UNCONFIGURED -> INACTIVE -> ACTIVE)
    else {
      // Configure if needed
      if (current_state == State::PRIMARY_STATE_UNCONFIGURED) {
        if (!sendLifecycleTransition(node_name, Transition::TRANSITION_CONFIGURE)) {
          RCLCPP_WARN(node_->get_logger(), "Failed to configure %s", node_name.c_str());
          return false;
        }

        current_state = State::PRIMARY_STATE_INACTIVE;
        if (current_state == target_state) {
          return true;
        }
      }

      // Activate if needed
      if (current_state == State::PRIMARY_STATE_INACTIVE &&
        target_state == State::PRIMARY_STATE_ACTIVE)
      {
        if (!sendLifecycleTransition(node_name, Transition::TRANSITION_ACTIVATE)) {
          RCLCPP_WARN(node_->get_logger(), "Failed to activate %s", node_name.c_str());
          return false;
        }

        return true;
      }
    }

    return true;

  } catch (const std::exception& e) {
    RCLCPP_WARN(
      node_->get_logger(),
      "Error transitioning %s: %s",
      node_name.c_str(), e.what());
    return false;
  }
}

}  // namespace maurice_nav
