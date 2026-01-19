#ifndef MAURICE_NAV__MODE_MANAGER_HPP_
#define MAURICE_NAV__MODE_MANAGER_HPP_

#include <string>
#include <vector>
#include <memory>
#include <unordered_map>

#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/string.hpp"
#include "nav_msgs/msg/odometry.hpp"
#include "geometry_msgs/msg/transform_stamped.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"

#include "brain_messages/srv/change_map.hpp"
#include "brain_messages/srv/change_navigation_mode.hpp"
#include "brain_messages/srv/save_map.hpp"
#include "brain_messages/srv/delete_map.hpp"
#include "nav2_msgs/srv/load_map.hpp"
#include "lifecycle_msgs/msg/state.hpp"

#include "maurice_nav/lifecycle_service_utils.hpp"

namespace maurice_nav
{

/**
 * @brief Navigation mode enumeration
 */
enum class NavigationMode
{
  NAV,       // Navigation mode with map
  MAPPING,   // SLAM mapping mode
  MAPFREE    // Map-free local navigation
};

/**
 * @brief Mode Manager node for orchestrating navigation lifecycle
 *
 * This node manages switching between navigation, mapping, and mapfree modes.
 * It handles lifecycle transitions for all navigation-related nodes and provides
 * services for mode switching, map management, and status monitoring.
 */
class ModeManager : public rclcpp::Node
{
public:
  /**
   * @brief Construct a new Mode Manager
   *
   * @param options Node options
   */
  explicit ModeManager(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());

  /**
   * @brief Destroy the Mode Manager
   */
  ~ModeManager();

private:
  // ========== Service Callbacks ==========

  /**
   * @brief Service callback to switch between navigation modes
   */
  void changeModeCallback(
    const std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Request> request,
    std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Response> response);

  /**
   * @brief Service callback to change the navigation map
   */
  void changeMapCallback(
    const std::shared_ptr<brain_messages::srv::ChangeMap::Request> request,
    std::shared_ptr<brain_messages::srv::ChangeMap::Response> response);

  /**
   * @brief Service callback to save the current map
   */
  void saveMapCallback(
    const std::shared_ptr<brain_messages::srv::SaveMap::Request> request,
    std::shared_ptr<brain_messages::srv::SaveMap::Response> response);

  /**
   * @brief Service callback to delete a map
   */
  void deleteMapCallback(
    const std::shared_ptr<brain_messages::srv::DeleteMap::Request> request,
    std::shared_ptr<brain_messages::srv::DeleteMap::Response> response);

  // ========== Core Operations ==========

  /**
   * @brief Request startup of navigation nodes for a given mode
   *
   * @param mode Target navigation mode
   * @return std::pair<bool, std::string> Success status and message
   */
  std::pair<bool, std::string> requestModeStartup(NavigationMode mode);

  /**
   * @brief Shutdown all nodes for a given mode
   *
   * @param mode_str Mode string (e.g., "navigation", "mapping", "mapfree")
   */
  void shutdownMode(const std::string& mode_str);

  /**
   * @brief Efficiently switch maps without full mode restart
   *
   * @return std::pair<bool, std::string> Success status and message
   */
  std::pair<bool, std::string> efficientMapSwitch();

  /**
   * @brief Load a map on the map server node
   *
   * @param node_name Name of the map server node
   * @param max_retries Maximum number of retry attempts
   * @return true if map loaded successfully
   */
  bool loadMapOnServer(const std::string& node_name, int max_retries = 20);

  // ========== Persistence ==========

  /**
   * @brief Load the last used mode from file
   *
   * @return std::string Last mode or "navigation" as default
   */
  std::string loadLastMode();

  /**
   * @brief Load the last used map from file
   *
   * @return std::string Last map name or empty if none
   */
  std::string loadLastMap();

  /**
   * @brief Save the current mode to file
   *
   * @param mode Mode string to save
   */
  void saveLastMode(const std::string& mode);

  /**
   * @brief Save the current map to file
   *
   * @param map_name Map name to save
   */
  void saveLastMap(const std::string& map_name);

  /**
   * @brief Discover available map files in the maps directory
   *
   * @return std::vector<std::string> List of map filenames
   */
  std::vector<std::string> discoverMaps();

  // ========== Utility Functions ==========

  /**
   * @brief Auto-start the mode manager in the saved mode
   */
  void autoStartMode();

  /**
   * @brief Publish status information (mode, maps, current map)
   */
  void publishStatus();

  /**
   * @brief Cleanup orphaned processes from previous runs
   */
  void cleanupOrphanedProcesses();

  /**
   * @brief Odometry callback for mapping_pose publishing
   */
  void odomCallback(const nav_msgs::msg::Odometry::SharedPtr msg);

  /**
   * @brief Internal helper for change_mode_callback with first_start flag
   */
  void changeModeCallbackInternal(
    const std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Request> request,
    std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Response> response,
    bool first_start);

  // ========== Member Variables ==========

  // Lifecycle management
  std::shared_ptr<LifecycleServiceClient> lifecycle_client_;

  // Callback groups
  rclcpp::CallbackGroup::SharedPtr calls_going_outside_group_;
  rclcpp::CallbackGroup::SharedPtr internal_callbacks_group_;

  // Services
  rclcpp::Service<brain_messages::srv::ChangeNavigationMode>::SharedPtr mode_service_;
  rclcpp::Service<brain_messages::srv::ChangeMap>::SharedPtr map_service_;
  rclcpp::Service<brain_messages::srv::SaveMap>::SharedPtr save_map_service_;
  rclcpp::Service<brain_messages::srv::DeleteMap>::SharedPtr delete_map_service_;

  // Publishers
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr mode_publisher_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr maps_publisher_;
  rclcpp::Publisher<std_msgs::msg::String>::SharedPtr current_map_publisher_;
  rclcpp::Publisher<nav_msgs::msg::Odometry>::SharedPtr mapping_pose_pub_;

  // Subscribers
  rclcpp::Subscription<nav_msgs::msg::Odometry>::SharedPtr odom_sub_;

  // Timers
  rclcpp::TimerBase::SharedPtr status_timer_;
  rclcpp::TimerBase::SharedPtr startup_timer_;

  // TF2
  std::shared_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;

  // State variables
  std::string current_mode_;
  std::string current_map_;
  std::vector<std::string> available_maps_;

  // File paths
  std::string maps_dir_;
  std::string mode_file_;
  std::string map_file_;

  // Mode configuration (static data, will be initialized in constructor)
  std::unordered_map<std::string, std::vector<std::string>> modes_nodes_;

  // Constants
  static constexpr const char* MAP_SERVER_NODE = "navigation_map_server";
  static constexpr const char* BT_NODE = "bt_navigator";
};

}  // namespace maurice_nav

#endif  // MAURICE_NAV__MODE_MANAGER_HPP_
