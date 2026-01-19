#include "maurice_nav/mode_manager.hpp"

#include <filesystem>
#include <fstream>
#include <algorithm>
#include <chrono>
#include <thread>
#include <glob.h>

#include "tf2/exceptions.h"
#include "tf2_ros/transform_listener.h"

namespace maurice_nav
{

ModeManager::ModeManager(const rclcpp::NodeOptions& options)
: Node("mode_manager", options)
{
  RCLCPP_INFO(get_logger(), "Initializing Mode Manager...");

  // Initialize callback groups
  calls_going_outside_group_ = create_callback_group(
    rclcpp::CallbackGroupType::Reentrant);
  internal_callbacks_group_ = create_callback_group(
    rclcpp::CallbackGroupType::Reentrant);

  // Initialize lifecycle client
  lifecycle_client_ = std::make_shared<LifecycleServiceClient>(
    shared_from_this(), calls_going_outside_group_);

  // Initialize mode configuration (matching Python's modes_nodes)
  modes_nodes_["mapping"] = {"slam_toolbox"};

  modes_nodes_["mapfree"] = {
    "null_map_node",
    "navigation/planner_server",
    "mapfree/planner_server",
    "controller_server",
    "bt_navigator",
    "behavior_server",
    "velocity_smoother"
  };

  modes_nodes_["navigation"] = {
    "navigation_map_server",
    "navigation_grid_localizer",
    "navigation_amcl",
    "mapfree/planner_server",
    "navigation/planner_server",
    "controller_server",
    "bt_navigator",
    "behavior_server",
    "velocity_smoother"
  };

  // Initialize paths
  const char* innate_os_root_env = std::getenv("INNATE_OS_ROOT");
  std::string maurice_root;
  if (innate_os_root_env != nullptr) {
    maurice_root = std::string(innate_os_root_env);
  } else {
    const char* home_env = std::getenv("HOME");
    if (home_env != nullptr) {
      maurice_root = std::string(home_env) + "/innate-os";
    } else {
      RCLCPP_ERROR(get_logger(), "Cannot determine HOME directory");
      maurice_root = "/tmp/innate-os";
    }
  }

  maps_dir_ = maurice_root + "/maps";
  mode_file_ = maurice_root + "/.last_mode";
  map_file_ = maurice_root + "/.last_map";

  // Create services
  mode_service_ = create_service<brain_messages::srv::ChangeNavigationMode>(
    "/nav/change_mode",
    std::bind(
      &ModeManager::changeModeCallback, this,
      std::placeholders::_1, std::placeholders::_2),
    rmw_qos_profile_services_default,
    internal_callbacks_group_);

  map_service_ = create_service<brain_messages::srv::ChangeMap>(
    "/nav/change_navigation_map",
    std::bind(
      &ModeManager::changeMapCallback, this,
      std::placeholders::_1, std::placeholders::_2),
    rmw_qos_profile_services_default,
    internal_callbacks_group_);

  save_map_service_ = create_service<brain_messages::srv::SaveMap>(
    "/nav/save_map",
    std::bind(
      &ModeManager::saveMapCallback, this,
      std::placeholders::_1, std::placeholders::_2),
    rmw_qos_profile_services_default,
    internal_callbacks_group_);

  delete_map_service_ = create_service<brain_messages::srv::DeleteMap>(
    "/nav/delete_map",
    std::bind(
      &ModeManager::deleteMapCallback, this,
      std::placeholders::_1, std::placeholders::_2),
    rmw_qos_profile_services_default,
    internal_callbacks_group_);

  // Create publishers
  mode_publisher_ = create_publisher<std_msgs::msg::String>("/nav/current_mode", 10);
  maps_publisher_ = create_publisher<std_msgs::msg::String>("/nav/available_maps", 10);
  current_map_publisher_ = create_publisher<std_msgs::msg::String>("/nav/current_map", 10);
  mapping_pose_pub_ = create_publisher<nav_msgs::msg::Odometry>("/mapping_pose", 10);

  // Create subscribers
  odom_sub_ = create_subscription<nav_msgs::msg::Odometry>(
    "/odom", 20,
    std::bind(&ModeManager::odomCallback, this, std::placeholders::_1));

  // Initialize TF2
  tf_buffer_ = std::make_shared<tf2_ros::Buffer>(get_clock());
  tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);

  // Discover available maps first
  available_maps_ = discoverMaps();

  // Load last mode or default to navigation
  current_mode_ = loadLastMode();

  // Load last map or default
  current_map_ = loadLastMap();

  // Create status timer (1 Hz)
  status_timer_ = create_wall_timer(
    std::chrono::seconds(1),
    std::bind(&ModeManager::publishStatus, this));

  // Log startup info
  RCLCPP_INFO(get_logger(), "Mode Manager starting with map management capabilities.");
  RCLCPP_INFO(get_logger(), "- Call /nav/change_mode service to switch modes (\"navigation\" or \"mapping\")");
  RCLCPP_INFO(get_logger(), "- Call /nav/change_navigation_map service to change map (restarts navigation if running)");
  RCLCPP_INFO(get_logger(), "- Call /nav/save_map service to save current map with new name (mapping mode only, set overwrite=true to replace existing maps)");
  RCLCPP_INFO(get_logger(), "- Call /nav/delete_map service to delete a saved map (cannot delete active map while navigation is running)");
  RCLCPP_INFO(get_logger(), "- Current mode: %s (loaded from persistence)", current_mode_.c_str());
  RCLCPP_INFO(get_logger(), "- Current map: %s (loaded from persistence)", current_map_.c_str());
  RCLCPP_INFO(get_logger(), "- Available maps: %zu maps", available_maps_.size());

  // Create startup timer (3 second delay)
  startup_timer_ = create_wall_timer(
    std::chrono::seconds(3),
    std::bind(&ModeManager::autoStartMode, this),
    internal_callbacks_group_);
}

ModeManager::~ModeManager()
{
  RCLCPP_INFO(get_logger(), "Destroying Mode Manager");
  try {
    cleanupOrphanedProcesses();
  } catch (const std::exception& e) {
    RCLCPP_WARN(get_logger(), "Error during cleanup in destructor: %s", e.what());
  }
}

std::vector<std::string> ModeManager::discoverMaps()
{
  std::vector<std::string> map_files;
  try {
    // Use glob to find .yaml files
    std::string pattern = maps_dir_ + "/*.yaml";
    glob_t glob_result;
    memset(&glob_result, 0, sizeof(glob_result));

    int return_value = glob(pattern.c_str(), GLOB_TILDE, nullptr, &glob_result);
    if (return_value == 0) {
      for (size_t i = 0; i < glob_result.gl_pathc; ++i) {
        std::filesystem::path p(glob_result.gl_pathv[i]);
        map_files.push_back(p.filename().string());
      }
    }
    globfree(&glob_result);

    std::sort(map_files.begin(), map_files.end());
    RCLCPP_INFO(get_logger(), "Discovered %zu maps", map_files.size());
  } catch (const std::exception& e) {
    RCLCPP_ERROR(get_logger(), "Error discovering maps: %s", e.what());
  }

  return map_files;
}

std::string ModeManager::loadLastMode()
{
  try {
    if (std::filesystem::exists(mode_file_)) {
      std::ifstream file(mode_file_);
      std::string saved_mode;
      std::getline(file, saved_mode);

      // Trim whitespace
      saved_mode.erase(0, saved_mode.find_first_not_of(" \t\n\r"));
      saved_mode.erase(saved_mode.find_last_not_of(" \t\n\r") + 1);

      if (saved_mode == "navigation" || saved_mode == "mapping" || saved_mode == "mapfree") {
        RCLCPP_INFO(get_logger(), "Loaded last mode: %s", saved_mode.c_str());
        return saved_mode;
      }
    }
    // Default to navigation mode
    RCLCPP_INFO(get_logger(), "No saved mode found, defaulting to navigation");
    return "navigation";
  } catch (const std::exception& e) {
    RCLCPP_ERROR(get_logger(), "Error loading last mode: %s, defaulting to navigation", e.what());
    return "navigation";
  }
}

std::string ModeManager::loadLastMap()
{
  try {
    if (std::filesystem::exists(map_file_)) {
      std::ifstream file(map_file_);
      std::string saved_map;
      std::getline(file, saved_map);

      // Trim whitespace
      saved_map.erase(0, saved_map.find_first_not_of(" \t\n\r"));
      saved_map.erase(saved_map.find_last_not_of(" \t\n\r") + 1);

      // Validate that the saved map exists
      if (!saved_map.empty() &&
        std::find(available_maps_.begin(), available_maps_.end(), saved_map) != available_maps_.end())
      {
        RCLCPP_INFO(get_logger(), "Loaded last map: %s", saved_map.c_str());
        return saved_map;
      } else {
        RCLCPP_WARN(get_logger(), "Saved map '%s' not found, defaulting", saved_map.c_str());
      }
    }

    // Default logic
    std::string default_map;
    if (std::find(available_maps_.begin(), available_maps_.end(), default_map) != available_maps_.end()) {
      RCLCPP_INFO(get_logger(), "No saved map found, defaulting to %s", default_map.c_str());
      return default_map;
    } else if (!available_maps_.empty()) {
      std::string first_map = available_maps_[0];
      RCLCPP_INFO(get_logger(), "Default map not found, using first available: %s", first_map.c_str());
      return first_map;
    } else {
      RCLCPP_WARN(get_logger(), "No maps available");
      return "";
    }
  } catch (const std::exception& e) {
    RCLCPP_ERROR(get_logger(), "Error loading last map: %s", e.what());
    return "";
  }
}

void ModeManager::saveLastMode(const std::string& mode)
{
  try {
    // Ensure directory exists
    std::filesystem::create_directories(std::filesystem::path(mode_file_).parent_path());

    std::ofstream file(mode_file_);
    file << mode;
    file.close();

    RCLCPP_DEBUG(get_logger(), "Saved mode: %s", mode.c_str());
  } catch (const std::exception& e) {
    RCLCPP_ERROR(get_logger(), "Error saving mode: %s", e.what());
  }
}

void ModeManager::saveLastMap(const std::string& map_name)
{
  try {
    // Ensure directory exists
    std::filesystem::create_directories(std::filesystem::path(map_file_).parent_path());

    std::ofstream file(map_file_);
    file << map_name;
    file.close();

    RCLCPP_DEBUG(get_logger(), "Saved map: %s", map_name.c_str());
  } catch (const std::exception& e) {
    RCLCPP_ERROR(get_logger(), "Error saving map: %s", e.what());
  }
}

void ModeManager::autoStartMode()
{
  RCLCPP_INFO(get_logger(), "Mode Manager auto-starting");
  startup_timer_->cancel();  // One-time execution

  cleanupOrphanedProcesses();

  // Check if we want to start navigation but have no maps available
  if (current_mode_ == "navigation" && available_maps_.empty()) {
    RCLCPP_WARN(get_logger(), "No maps available for navigation - automatically starting in mapping mode");
    RCLCPP_INFO(get_logger(), "Create a map first, then switch to navigation mode");
    current_mode_ = "mapping";
  }

  if (current_mode_ == "navigation" || current_mode_ == "mapping") {
    RCLCPP_INFO(get_logger(), "Auto-starting in %s mode...", current_mode_.c_str());
    auto request = std::make_shared<brain_messages::srv::ChangeNavigationMode::Request>();
    request->mode = current_mode_;
    auto response = std::make_shared<brain_messages::srv::ChangeNavigationMode::Response>();
    changeModeCallbackInternal(request, response, true);
  } else if (current_mode_ == "mapfree") {
    RCLCPP_INFO(get_logger(), "Auto-starting in mapfree mode (local Nav2)");
    auto request = std::make_shared<brain_messages::srv::ChangeNavigationMode::Request>();
    request->mode = current_mode_;
    auto response = std::make_shared<brain_messages::srv::ChangeNavigationMode::Response>();
    changeModeCallbackInternal(request, response, true);
  }
}

void ModeManager::publishStatus()
{
  // Publish current mode
  auto mode_msg = std_msgs::msg::String();
  mode_msg.data = current_mode_;
  mode_publisher_->publish(mode_msg);

  // Publish available maps as JSON
  std::string maps_json = "{\"available_maps\": [";
  for (size_t i = 0; i < available_maps_.size(); ++i) {
    maps_json += "\"" + available_maps_[i] + "\"";
    if (i < available_maps_.size() - 1) {
      maps_json += ", ";
    }
  }
  maps_json += "]}";

  auto maps_msg = std_msgs::msg::String();
  maps_msg.data = maps_json;
  maps_publisher_->publish(maps_msg);

  // Publish current map
  auto current_map_msg = std_msgs::msg::String();
  current_map_msg.data = current_map_.empty() ? "" : current_map_;
  current_map_publisher_->publish(current_map_msg);
}

void ModeManager::odomCallback(const nav_msgs::msg::Odometry::SharedPtr msg)
{
  // Only publish mapping_pose in mapping mode
  if (current_mode_ != "mapping") {
    return;
  }

  try {
    auto transform = tf_buffer_->lookupTransform(
      "map", "base_link",
      tf2::TimePointZero);

    auto odom_msg = nav_msgs::msg::Odometry();
    odom_msg.header.stamp = msg->header.stamp;
    odom_msg.header.frame_id = "map";
    odom_msg.child_frame_id = "base_link";
    odom_msg.pose.pose.position.x = transform.transform.translation.x;
    odom_msg.pose.pose.position.y = transform.transform.translation.y;
    odom_msg.pose.pose.position.z = transform.transform.translation.z;
    odom_msg.pose.pose.orientation = transform.transform.rotation;

    // Set covariance and twist to zeros
    std::fill(odom_msg.pose.covariance.begin(), odom_msg.pose.covariance.end(), 0.0);
    odom_msg.twist.twist.linear.x = 0.0;
    odom_msg.twist.twist.linear.y = 0.0;
    odom_msg.twist.twist.linear.z = 0.0;
    odom_msg.twist.twist.angular.x = 0.0;
    odom_msg.twist.twist.angular.y = 0.0;
    odom_msg.twist.twist.angular.z = 0.0;
    std::fill(odom_msg.twist.covariance.begin(), odom_msg.twist.covariance.end(), 0.0);

    mapping_pose_pub_->publish(odom_msg);
  } catch (const tf2::TransformException& e) {
    RCLCPP_WARN_THROTTLE(
      get_logger(), *get_clock(), 5000,
      "[mapping_pose] TF lookup failed in odom_callback: %s", e.what());
  }
}

void ModeManager::shutdownMode(const std::string& mode_str)
{
  auto it = modes_nodes_.find(mode_str);
  if (it == modes_nodes_.end()) {
    RCLCPP_DEBUG(get_logger(), "Mode '%s' not found in modes_nodes", mode_str.c_str());
    return;
  }

  const auto& nodes = it->second;
  if (nodes.empty()) {
    RCLCPP_DEBUG(get_logger(), "No nodes configured for mode '%s'", mode_str.c_str());
    return;
  }

  // Iterate in reverse order for shutdown
  for (auto rit = nodes.rbegin(); rit != nodes.rend(); ++rit) {
    const std::string& node_name = *rit;
    try {
      // Get current state of the node
      auto current_state_opt = lifecycle_client_->getNodeState(node_name);

      if (!current_state_opt.has_value()) {
        RCLCPP_WARN(get_logger(), "Failed to get state for %s", node_name.c_str());
        continue;
      }

      uint8_t current_state_id = current_state_opt.value();
      RCLCPP_INFO(get_logger(), "%s state %d", node_name.c_str(), current_state_id);

      // Transition to UNCONFIGURED (handles both ACTIVE and INACTIVE)
      if (current_state_id != lifecycle_msgs::msg::State::PRIMARY_STATE_UNCONFIGURED) {
        RCLCPP_INFO(get_logger(), "Shutting down %s", node_name.c_str());
        bool success = lifecycle_client_->transitionNode(
          node_name, lifecycle_msgs::msg::State::PRIMARY_STATE_UNCONFIGURED);
        if (success) {
          RCLCPP_INFO(get_logger(), "Shut down %s", node_name.c_str());
        } else {
          RCLCPP_WARN(get_logger(), "Failed to shut down %s", node_name.c_str());
        }
      } else {
        RCLCPP_DEBUG(get_logger(), "Node %s already unconfigured, skipping", node_name.c_str());
      }
    } catch (const std::exception& e) {
      RCLCPP_DEBUG(get_logger(), "Error shutting down %s: %s", node_name.c_str(), e.what());
    }
  }
}

std::pair<bool, std::string> ModeManager::requestModeStartup(NavigationMode mode)
{
  std::string mode_value;
  switch (mode) {
    case NavigationMode::NAV:
      mode_value = "navigation";
      break;
    case NavigationMode::MAPPING:
      mode_value = "mapping";
      break;
    case NavigationMode::MAPFREE:
      mode_value = "mapfree";
      break;
  }

  try {
    RCLCPP_INFO(get_logger(), "Requesting %s mode startup", mode_value.c_str());

    // Collect all nodes except target mode nodes
    std::vector<std::string> all_nodes_except_target;
    for (const auto& [mode_name, nodes] : modes_nodes_) {
      for (const auto& node : nodes) {
        all_nodes_except_target.push_back(node);
      }
    }

    // Remove target mode nodes from the list
    auto it = modes_nodes_.find(mode_value);
    if (it == modes_nodes_.end()) {
      std::string msg = "No nodes configured for mode '" + mode_value + "'";
      RCLCPP_ERROR(get_logger(), "%s", msg.c_str());
      return {false, msg};
    }

    const auto& target_nodes = it->second;
    for (const auto& node : target_nodes) {
      auto pos = std::find(all_nodes_except_target.begin(), all_nodes_except_target.end(), node);
      if (pos != all_nodes_except_target.end()) {
        all_nodes_except_target.erase(pos);
      }
    }

    // Shutdown all other nodes
    std::vector<std::string> failures;
    for (const auto& node_name : all_nodes_except_target) {
      bool success = lifecycle_client_->transitionNode(
        node_name, lifecycle_msgs::msg::State::PRIMARY_STATE_UNCONFIGURED);
      if (!success) {
        failures.push_back(node_name);
        RCLCPP_WARN(get_logger(), "Failed to transition %s to unconfigured", node_name.c_str());
      }
    }

    // Phase 1: Configure all nodes in forward order
    RCLCPP_INFO(get_logger(), "Configuring %zu nodes for %s mode", target_nodes.size(), mode_value.c_str());
    for (const auto& node_name : target_nodes) {
      bool success = lifecycle_client_->transitionNode(
        node_name, lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE, true);
      if (!success) {
        failures.push_back(node_name);
        RCLCPP_WARN(get_logger(), "Failed to configure %s, continuing...", node_name.c_str());
      }
    }
    RCLCPP_INFO(get_logger(), "Configured nodes");

    // Phase 2: Activate nodes in forward order
    bool map_load_success = false;
    RCLCPP_INFO(get_logger(), "Activating %zu nodes for %s mode", target_nodes.size(), mode_value.c_str());
    for (const auto& node_name : target_nodes) {
      if (std::find(failures.begin(), failures.end(), node_name) != failures.end()) {
        RCLCPP_WARN(get_logger(), "%s failed configuration. Not proceeding further.", node_name.c_str());
        break;
      }

      bool success = lifecycle_client_->transitionNode(
        node_name, lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE);
      if (!success) {
        failures.push_back(node_name);
        RCLCPP_WARN(get_logger(), "Failed to activate %s. Not proceeding further.", node_name.c_str());
        break;
      }

      // Load map immediately after map server is activated (navigation mode only)
      if (mode == NavigationMode::NAV && node_name.find("map_server") != std::string::npos && success) {
        map_load_success = loadMapOnServer(node_name);
        if (!map_load_success) {
          failures.push_back(node_name + "_map_load");
        }
      }
    }
    RCLCPP_INFO(get_logger(), "Activated nodes");

    if (!failures.empty()) {
      std::string failures_str;
      for (size_t i = 0; i < failures.size(); ++i) {
        failures_str += failures[i];
        if (i < failures.size() - 1) failures_str += ", ";
      }
      std::string message = mode_value + " mode started with " +
        std::to_string(failures.size()) + " activation failures: " + failures_str;
      RCLCPP_WARN(get_logger(), "%s", message.c_str());
      return {false, message};
    } else {
      std::string map_status = mode == NavigationMode::NAV ?
        (map_load_success ? "loaded" : "not loaded") : "N/A";
      std::string message = mode_value + " mode started successfully (map: " + map_status + ")";
      RCLCPP_INFO(get_logger(), "%s", message.c_str());
      return {true, message};
    }

  } catch (const std::exception& e) {
    std::string error_msg = "Error requesting " + mode_value + " startup: " + std::string(e.what());
    RCLCPP_ERROR(get_logger(), "%s", error_msg.c_str());
    return {false, error_msg};
  }
}

bool ModeManager::loadMapOnServer(const std::string& node_name, int max_retries)
{
  if (node_name.empty() || current_map_.empty()) {
    return false;
  }

  auto map_request = std::make_shared<nav2_msgs::srv::LoadMap::Request>();
  map_request->map_url = maps_dir_ + "/" + current_map_;

  RCLCPP_INFO(get_logger(), "Loading map: %s on %s", current_map_.c_str(), node_name.c_str());

  int retry_count = 0;
  while (retry_count <= max_retries) {
    std::string service_name = "/" + node_name + "/load_map";
    auto map_result = lifecycle_client_->callService<nav2_msgs::srv::LoadMap>(
      service_name, map_request, 5.0);

    if (map_result.has_value() && (*map_result)->result == 0) {
      RCLCPP_INFO(get_logger(), "Map loaded successfully after %d attempt(s)", retry_count + 1);
      return true;
    } else {
      retry_count++;
      if (retry_count <= max_retries) {
        RCLCPP_WARN(get_logger(), "Map load attempt %d failed, retrying...", retry_count);
        std::this_thread::sleep_for(std::chrono::milliseconds(250));
      }
    }
  }

  RCLCPP_ERROR(get_logger(), "Failed to load map after %d attempts", max_retries);
  return false;
}

std::pair<bool, std::string> ModeManager::efficientMapSwitch()
{
  auto it = modes_nodes_.find("navigation");
  if (it == modes_nodes_.end()) {
    return {false, "No navigation nodes configured"};
  }

  const auto& nodes = it->second;
  std::vector<std::string> failures;

  // Step 1: Transition bt_navigator down to inactive
  RCLCPP_INFO(get_logger(), "Step 1: Transitioning %s to inactive", BT_NODE);
  bool success = lifecycle_client_->transitionNode(
    BT_NODE, lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE);
  if (!success) {
    failures.push_back("bt_navigator");
    RCLCPP_WARN(get_logger(), "Failed to transition bt_navigator down");
  }

  // Step 2: Load new map
  RCLCPP_INFO(get_logger(), "Step 2: Loading new map");
  bool map_load_success = loadMapOnServer(MAP_SERVER_NODE);
  if (!map_load_success) {
    failures.push_back(std::string(MAP_SERVER_NODE) + "_map_load");
  }

  // Step 3: Transition all nodes to active
  RCLCPP_INFO(get_logger(), "Step 3: Activating all nodes");
  for (const auto& node_name : nodes) {
    bool success = lifecycle_client_->transitionNode(
      node_name, lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE, true);
    if (!success) {
      failures.push_back(node_name);
      RCLCPP_WARN(get_logger(), "Failed to activate %s", node_name.c_str());
    }
  }

  if (!failures.empty()) {
    std::string failures_str;
    for (size_t i = 0; i < failures.size(); ++i) {
      failures_str += failures[i];
      if (i < failures.size() - 1) failures_str += ", ";
    }
    return {false, "Map switch completed with failures: " + failures_str};
  } else {
    return {true, "Map switched successfully to " + current_map_};
  }
}

void ModeManager::cleanupOrphanedProcesses()
{
  try {
    RCLCPP_INFO(get_logger(), "Attempting to shutdown all modes...");
    shutdownMode("navigation");
    shutdownMode("mapping");
    shutdownMode("mapfree");
    RCLCPP_INFO(get_logger(), "Cleaned up all modes");
  } catch (const std::exception& e) {
    RCLCPP_WARN(get_logger(), "Cleanup warning: %s", e.what());
  }
}

void ModeManager::changeModeCallback(
  const std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Request> request,
  std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Response> response)
{
  changeModeCallbackInternal(request, response, false);
}

void ModeManager::changeModeCallbackInternal(
  const std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Request> request,
  std::shared_ptr<brain_messages::srv::ChangeNavigationMode::Response> response,
  bool first_start)
{
  RCLCPP_INFO(get_logger(), "Attempting to change mode");

  try {
    std::string target_mode = request->mode;
    // Trim and convert to lowercase
    target_mode.erase(0, target_mode.find_first_not_of(" \t\n\r"));
    target_mode.erase(target_mode.find_last_not_of(" \t\n\r") + 1);
    std::transform(target_mode.begin(), target_mode.end(), target_mode.begin(), ::tolower);

    if (current_map_.empty()) {
      target_mode = "mapping";
    }

    // Validate mode
    if (target_mode != "navigation" && target_mode != "mapping" && target_mode != "mapfree") {
      response->success = false;
      response->message = "Invalid mode '" + target_mode + "'. Use 'navigation', 'mapping', or 'mapfree'";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Check if trying to switch to navigation but no maps are available
    if (target_mode == "navigation" && available_maps_.empty()) {
      response->success = false;
      response->message = "Cannot switch to navigation mode - no maps available. Create a map first using mapping mode.";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Don't restart if already in the requested mode
    if (current_mode_ == target_mode && !first_start) {
      response->success = true;
      response->message = "Already in " + target_mode + " mode";
      RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Set mode to switching
    current_mode_ = "switching";
    publishStatus();  // Immediately publish the change

    // For mapfree mode
    if (target_mode == "mapfree") {
      RCLCPP_INFO(get_logger(), "Starting mapfree local navigation stack...");
      auto [success, message] = requestModeStartup(NavigationMode::MAPFREE);

      if (success) {
        response->success = true;
        response->message = "Switched to mapfree mode (local Nav2 running)";
        current_mode_ = "mapfree";
        saveLastMode("mapfree");
      } else {
        response->success = false;
        response->message = "Failed to start mapfree local navigation: " + message;
        current_mode_ = "none";
      }

      RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Map target_mode string to NavigationMode enum
    NavigationMode target_mode_enum;
    if (target_mode == "navigation") {
      target_mode_enum = NavigationMode::NAV;
    } else if (target_mode == "mapping") {
      target_mode_enum = NavigationMode::MAPPING;
    } else {
      // Should not reach here due to validation above
      response->success = false;
      response->message = "Invalid mode";
      current_mode_ = "none";
      return;
    }

    RCLCPP_INFO(get_logger(), "Starting %s mode...", target_mode.c_str());
    auto [success, message] = requestModeStartup(target_mode_enum);

    if (success) {
      response->success = true;
      response->message = "Successfully switched to " + target_mode + " mode";
      if (target_mode == "navigation") {
        response->message += " with map '" + current_map_ + "'";
      }
      current_mode_ = target_mode;
      saveLastMode(target_mode);
      RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
    } else {
      response->success = false;
      response->message = "Failed to start " + target_mode + " mode: " + message;
      current_mode_ = "none";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
    }

  } catch (const std::exception& e) {
    response->success = false;
    response->message = "Error switching modes: " + std::string(e.what());
    RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
    current_mode_ = "none";
  }

  RCLCPP_INFO(get_logger(), "returning from change mode callback");
}

void ModeManager::changeMapCallback(
  const std::shared_ptr<brain_messages::srv::ChangeMap::Request> request,
  std::shared_ptr<brain_messages::srv::ChangeMap::Response> response)
{
  try {
    std::string requested_map = request->map_name;
    // Trim whitespace
    requested_map.erase(0, requested_map.find_first_not_of(" \t\n\r"));
    requested_map.erase(requested_map.find_last_not_of(" \t\n\r") + 1);

    // Validate that the requested map exists
    if (std::find(available_maps_.begin(), available_maps_.end(), requested_map) == available_maps_.end()) {
      response->success = false;
      std::string available_str;
      for (size_t i = 0; i < available_maps_.size(); ++i) {
        available_str += available_maps_[i];
        if (i < available_maps_.size() - 1) available_str += ", ";
      }
      response->message = "Error: Map '" + requested_map + "' not found. Available maps: " + available_str;
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Update the current map
    current_map_ = requested_map;

    // Save the new map choice for persistence
    saveLastMap(requested_map);

    // If we're in navigation mode, use efficient map switch
    if (current_mode_ == "navigation") {
      RCLCPP_INFO(get_logger(), "Efficiently switching to new map: %s", requested_map.c_str());

      auto [success, message] = efficientMapSwitch();

      if (success) {
        response->success = true;
        response->message = "Successfully changed map to '" + requested_map + "'";
        RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
      } else {
        response->success = false;
        response->message = "Failed to switch to map '" + requested_map + "': " + message;
        RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      }
    } else {
      // If not in navigation mode, just update the map for next time navigation starts
      response->success = true;
      response->message = "Map set to '" + requested_map + "' for next navigation session";
      RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
    }

  } catch (const std::exception& e) {
    response->success = false;
    response->message = "Error changing map: " + std::string(e.what());
    RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
  }
}

void ModeManager::saveMapCallback(
  const std::shared_ptr<brain_messages::srv::SaveMap::Request> request,
  std::shared_ptr<brain_messages::srv::SaveMap::Response> response)
{
  try {
    std::string map_name = request->map_name;
    // Trim whitespace
    map_name.erase(0, map_name.find_first_not_of(" \t\n\r"));
    map_name.erase(map_name.find_last_not_of(" \t\n\r") + 1);

    // Validate we're in mapping mode
    if (current_mode_ != "mapping") {
      response->success = false;
      response->message = "Cannot save map - not in mapping mode. Current mode: " + current_mode_;
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Validate map name (alphanumeric, underscores, hyphens only)
    bool valid_name = !map_name.empty();
    for (char c : map_name) {
      if (!std::isalnum(c) && c != '_' && c != '-') {
        valid_name = false;
        break;
      }
    }

    if (!valid_name) {
      response->success = false;
      response->message = "Invalid map name '" + map_name + "'. Use alphanumeric characters, underscores, and hyphens only.";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Check if map already exists
    std::string map_yaml_name = map_name + ".yaml";
    bool is_overwriting = std::find(available_maps_.begin(), available_maps_.end(), map_yaml_name) != available_maps_.end();

    if (is_overwriting) {
      if (!request->overwrite) {
        response->success = false;
        response->message = "Map '" + map_yaml_name + "' already exists. Set overwrite=true to replace it, or choose a different name.";
        RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
        return;
      } else {
        RCLCPP_INFO(get_logger(), "Overwriting existing map: %s", map_yaml_name.c_str());
        // Remove old files before saving new ones
        try {
          std::filesystem::path old_yaml = std::filesystem::path(maps_dir_) / map_yaml_name;
          std::filesystem::path old_pgm = std::filesystem::path(maps_dir_) / (map_name + ".pgm");
          if (std::filesystem::exists(old_yaml)) {
            std::filesystem::remove(old_yaml);
          }
          if (std::filesystem::exists(old_pgm)) {
            std::filesystem::remove(old_pgm);
          }
          RCLCPP_INFO(get_logger(), "Removed old map files for: %s", map_name.c_str());
        } catch (const std::exception& e) {
          RCLCPP_WARN(get_logger(), "Could not remove old map files: %s", e.what());
        }
      }
    }

    // Ensure maps directory exists
    std::filesystem::create_directories(maps_dir_);

    // Create full path for the map (without extension)
    std::string map_path = maps_dir_ + "/" + map_name;

    RCLCPP_INFO(get_logger(), "Saving current map as: %s", map_name.c_str());

    // Use nav2_map_server map_saver_cli to save the map
    std::string save_cmd = "ros2 run nav2_map_server map_saver_cli -f " + map_path +
      " --ros-args -p save_map_timeout:=5000.0";

    int result = std::system(save_cmd.c_str());

    if (result == 0) {
      // Check if the files were actually created
      std::string yaml_file = map_path + ".yaml";
      std::string pgm_file = map_path + ".pgm";

      if (std::filesystem::exists(yaml_file) && std::filesystem::exists(pgm_file)) {
        response->success = true;
        std::string action_word = is_overwriting ? "overwritten" : "saved";
        response->message = "Successfully " + action_word + " map as '" + map_name + ".yaml'";
        RCLCPP_INFO(get_logger(), "%s", response->message.c_str());

        // Refresh available maps list
        available_maps_ = discoverMaps();
        RCLCPP_INFO(get_logger(), "Updated available maps: %zu maps", available_maps_.size());
      } else {
        response->success = false;
        response->message = "Map saver completed but files not found at " + map_path;
        RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      }
    } else {
      response->success = false;
      response->message = "Map saver failed with return code " + std::to_string(result);
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
    }

  } catch (const std::exception& e) {
    response->success = false;
    response->message = "Error saving map: " + std::string(e.what());
    RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
  }
}

void ModeManager::deleteMapCallback(
  const std::shared_ptr<brain_messages::srv::DeleteMap::Request> request,
  std::shared_ptr<brain_messages::srv::DeleteMap::Response> response)
{
  try {
    std::string requested_name = request->map_name;
    // Trim whitespace
    requested_name.erase(0, requested_name.find_first_not_of(" \t\n\r"));
    requested_name.erase(requested_name.find_last_not_of(" \t\n\r") + 1);

    if (requested_name.empty()) {
      response->success = false;
      response->message = "Map name cannot be empty";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Normalize to YAML filename
    std::string map_yaml_name = requested_name;
    if (map_yaml_name.find(".yaml") == std::string::npos) {
      map_yaml_name += ".yaml";
    }

    // Validate that the map exists
    if (std::find(available_maps_.begin(), available_maps_.end(), map_yaml_name) == available_maps_.end()) {
      response->success = false;
      std::string available_str;
      for (size_t i = 0; i < available_maps_.size(); ++i) {
        available_str += available_maps_[i];
        if (i < available_maps_.size() - 1) available_str += ", ";
      }
      response->message = "Error: Map '" + map_yaml_name + "' not found. Available maps: " + available_str;
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Prevent deleting the active map while navigation is running
    if (map_yaml_name == current_map_ && current_mode_ == "navigation") {
      response->success = false;
      response->message = "Cannot delete the active map '" + map_yaml_name + "' while navigation is running. Change map or stop navigation first.";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // If we are deleting the current map (but not running navigation), pick a fallback
    if (map_yaml_name == current_map_) {
      std::vector<std::string> remaining_maps;
      for (const auto& m : available_maps_) {
        if (m != map_yaml_name) {
          remaining_maps.push_back(m);
        }
      }

      std::string fallback = "home.yaml";  // Default placeholder
      if (std::find(remaining_maps.begin(), remaining_maps.end(), "home.yaml") != remaining_maps.end()) {
        fallback = "home.yaml";
      } else if (!remaining_maps.empty()) {
        fallback = remaining_maps[0];
      }

      current_map_ = fallback;
      saveLastMap(fallback);
      RCLCPP_INFO(get_logger(), "Current map changed to '%s' prior to deletion of '%s'",
        fallback.c_str(), map_yaml_name.c_str());
    }

    // Delete files (.yaml and associated image files)
    std::string base_name = map_yaml_name.substr(0, map_yaml_name.find(".yaml"));
    std::filesystem::path yaml_path = std::filesystem::path(maps_dir_) / map_yaml_name;
    std::filesystem::path pgm_path = std::filesystem::path(maps_dir_) / (base_name + ".pgm");
    std::filesystem::path png_path = std::filesystem::path(maps_dir_) / (base_name + ".png");

    bool removed_any = false;
    for (const auto& path : {yaml_path, pgm_path, png_path}) {
      try {
        if (std::filesystem::exists(path)) {
          std::filesystem::remove(path);
          removed_any = true;
        }
      } catch (const std::exception& e) {
        RCLCPP_WARN(get_logger(), "Could not remove '%s': %s", path.c_str(), e.what());
      }
    }

    if (!removed_any) {
      response->success = false;
      response->message = "No files found to delete for map '" + map_yaml_name + "'";
      RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
      return;
    }

    // Refresh available maps list
    available_maps_ = discoverMaps();

    response->success = true;
    response->message = "Successfully deleted map '" + map_yaml_name + "'";
    RCLCPP_INFO(get_logger(), "%s", response->message.c_str());

  } catch (const std::exception& e) {
    response->success = false;
    response->message = "Error deleting map: " + std::string(e.what());
    RCLCPP_ERROR(get_logger(), "%s", response->message.c_str());
  }
}

}  // namespace maurice_nav
