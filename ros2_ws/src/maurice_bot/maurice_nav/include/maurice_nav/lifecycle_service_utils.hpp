#ifndef MAURICE_NAV__LIFECYCLE_SERVICE_UTILS_HPP_
#define MAURICE_NAV__LIFECYCLE_SERVICE_UTILS_HPP_

#include <string>
#include <memory>
#include <optional>
#include <chrono>
#include <unordered_map>

#include "rclcpp/rclcpp.hpp"
#include "lifecycle_msgs/msg/state.hpp"
#include "lifecycle_msgs/msg/transition.hpp"
#include "lifecycle_msgs/srv/get_state.hpp"
#include "lifecycle_msgs/srv/change_state.hpp"

namespace maurice_nav
{

/**
 * @brief Client wrapper for lifecycle service operations
 *
 * This class provides utilities for interacting with ROS2 lifecycle nodes,
 * including getting node state, sending transitions, and managing state changes.
 */
class LifecycleServiceClient
{
public:
  /**
   * @brief Construct a new Lifecycle Service Client
   *
   * @param node Pointer to the parent ROS2 node
   * @param callback_group Callback group for service clients
   */
  explicit LifecycleServiceClient(
    rclcpp::Node::SharedPtr node,
    rclcpp::CallbackGroup::SharedPtr callback_group);

  /**
   * @brief Get the current state of a lifecycle node
   *
   * @param node_name Name of the lifecycle node
   * @param timeout_sec Timeout in seconds (default: 5.0)
   * @return std::optional<uint8_t> State ID if successful, std::nullopt otherwise
   */
  std::optional<uint8_t> getNodeState(
    const std::string& node_name,
    double timeout_sec = 5.0);

  /**
   * @brief Send a single lifecycle transition to a node
   *
   * @param node_name Name of the lifecycle node
   * @param transition_id Transition ID to execute
   * @param timeout_sec Timeout in seconds (default: 8.0)
   * @return true if transition succeeded
   * @return false if transition failed
   */
  bool sendLifecycleTransition(
    const std::string& node_name,
    uint8_t transition_id,
    double timeout_sec = 8.0);

  /**
   * @brief Transition a node to a target state
   *
   * Intelligently transitions up or down as needed to reach the target state.
   *
   * @param node_name Name of the lifecycle node
   * @param target_state Target state ID (UNCONFIGURED, INACTIVE, or ACTIVE)
   * @param only_up If true, only allow upward transitions (default: false)
   * @return true if transition succeeded
   * @return false if transition failed
   */
  bool transitionNode(
    const std::string& node_name,
    uint8_t target_state,
    bool only_up = false);

  /**
   * @brief Generic service call with timeout
   *
   * @tparam ServiceT Service type
   * @param service_name Full service name (e.g., "/node_name/get_state")
   * @param request Service request
   * @param timeout_sec Timeout in seconds (default: 2.0)
   * @return std::optional<typename ServiceT::Response::SharedPtr> Response if successful
   */
  template<typename ServiceT>
  std::optional<typename ServiceT::Response::SharedPtr> callService(
    const std::string& service_name,
    typename ServiceT::Request::SharedPtr request,
    double timeout_sec = 2.0);

private:
  /**
   * @brief Get or create a service client for a given service
   *
   * @tparam ServiceT Service type
   * @param service_name Full service name
   * @return std::shared_ptr<rclcpp::Client<ServiceT>> Service client
   */
  template<typename ServiceT>
  std::shared_ptr<rclcpp::Client<ServiceT>> getOrCreateClient(
    const std::string& service_name);

  rclcpp::Node::SharedPtr node_;
  rclcpp::CallbackGroup::SharedPtr callback_group_;

  // Store service clients to avoid recreating them
  std::unordered_map<std::string, rclcpp::ClientBase::SharedPtr> service_clients_;
};

// Template implementation must be in header
template<typename ServiceT>
std::optional<typename ServiceT::Response::SharedPtr> LifecycleServiceClient::callService(
  const std::string& service_name,
  typename ServiceT::Request::SharedPtr request,
  double timeout_sec)
{
  auto client = getOrCreateClient<ServiceT>(service_name);

  try {
    // Wait for service to be available
    if (!client->wait_for_service(std::chrono::duration<double>(timeout_sec))) {
      RCLCPP_INFO(node_->get_logger(), "Service '%s' not available", service_name.c_str());
      return std::nullopt;
    }

    // Send async request
    auto future = client->async_send_request(request);
    auto start_time = std::chrono::steady_clock::now();

    // Poll for result with timeout
    while (rclcpp::ok()) {
      auto elapsed = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - start_time).count();

      if (elapsed >= timeout_sec) {
        RCLCPP_WARN(
          node_->get_logger(),
          "Timeout waiting for service '%s' after %.2fs",
          service_name.c_str(), elapsed);
        return std::nullopt;
      }

      auto status = future.wait_for(std::chrono::milliseconds(100));
      if (status == std::future_status::ready) {
        auto result = future.get();
        if (result != nullptr) {
          return result;
        } else {
          RCLCPP_INFO(node_->get_logger(), "Service '%s' returned nullptr", service_name.c_str());
          return std::nullopt;
        }
      }
    }

    return std::nullopt;

  } catch (const std::exception& e) {
    RCLCPP_WARN(
      node_->get_logger(),
      "Exception calling service '%s': %s",
      service_name.c_str(), e.what());
    return std::nullopt;
  }
}

template<typename ServiceT>
std::shared_ptr<rclcpp::Client<ServiceT>> LifecycleServiceClient::getOrCreateClient(
  const std::string& service_name)
{
  // Check if client already exists
  auto it = service_clients_.find(service_name);
  if (it != service_clients_.end()) {
    return std::static_pointer_cast<rclcpp::Client<ServiceT>>(it->second);
  }

  // Create new client
  auto client = node_->create_client<ServiceT>(service_name, rmw_qos_profile_services_default, callback_group_);
  service_clients_[service_name] = client;

  return client;
}

}  // namespace maurice_nav

#endif  // MAURICE_NAV__LIFECYCLE_SERVICE_UTILS_HPP_
