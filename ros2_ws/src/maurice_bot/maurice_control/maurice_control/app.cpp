/**
 * AppControl node for MARS robot.
 * Handles joystick input, leader arm control, and robot info publishing.
 */

#include <rclcpp/rclcpp.hpp>
#include <geometry_msgs/msg/vector3.hpp>
#include <geometry_msgs/msg/twist.hpp>
#include <std_msgs/msg/int32_multi_array.hpp>
#include <std_msgs/msg/float64_multi_array.hpp>
#include <std_msgs/msg/string.hpp>
#include <maurice_msgs/srv/set_robot_name.hpp>
#include <maurice_msgs/srv/trigger_update.hpp>

#include <nlohmann/json.hpp>
#include <yaml-cpp/yaml.h>
#include <filesystem>
#include <fstream>
#include <cmath>
#include <array>
#include <chrono>
#include <cstdlib>
#include <memory>
#include <regex>
#include <sstream>
#include <signal.h>

using json = nlohmann::json;
using namespace std::chrono_literals;

namespace maurice_control {

/**
 * Execute a shell command and return its output (trimmed).
 */
std::string exec_command(const std::string& cmd) {
    std::array<char, 256> buffer;
    std::string result;
    std::unique_ptr<FILE, decltype(&pclose)> pipe(popen(cmd.c_str(), "r"), pclose);
    if (!pipe) {
        return "";
    }
    while (fgets(buffer.data(), buffer.size(), pipe.get()) != nullptr) {
        result += buffer.data();
    }
    // Trim trailing newline
    while (!result.empty() && (result.back() == '\n' || result.back() == '\r')) {
        result.pop_back();
    }
    return result;
}

/**
 * Get the Bluetooth device ID (MAC address) of the first available adapter.
 * Returns the MAC address as a string, or empty string if not available.
 */
std::string get_bluetooth_device_id() {
    try {
        // Use bluetoothctl to get the default adapter address
        std::string result = exec_command("bluetoothctl show 2>/dev/null | grep 'Controller' | head -1 | awk '{print $2}'");

        // Validate MAC address format
        std::regex mac_regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$");
        if (std::regex_match(result, mac_regex)) {
            return result;
        }
        return "";
    } catch (...) {
        return "";
    }
}

/**
 * Get the subject (first line) of the latest tag's annotation message.
 * Returns empty string if no tags or tag has no message.
 */
std::string get_tag_subject(const std::string& maurice_root) {
    // Get latest tag
    std::string tags_cmd = "cd \"" + maurice_root + "\" && git tag --list --sort=-version:refname 2>/dev/null | head -1";
    std::string latest_tag = exec_command(tags_cmd);
    
    if (latest_tag.empty()) {
        return "";
    }
    
    // Get the tag message subject (first line only)
    std::string subject_cmd = "cd \"" + maurice_root + "\" && git tag -l --format='%(contents:subject)' \"" + latest_tag + "\" 2>/dev/null";
    return exec_command(subject_cmd);
}

/**
 * Get the body (everything after first line) of the latest tag's annotation message.
 * Returns empty string if no tags or tag has no body.
 */
std::string get_tag_body(const std::string& maurice_root) {
    // Get latest tag
    std::string tags_cmd = "cd \"" + maurice_root + "\" && git tag --list --sort=-version:refname 2>/dev/null | head -1";
    std::string latest_tag = exec_command(tags_cmd);
    
    if (latest_tag.empty()) {
        return "";
    }
    
    // Get the tag message body (everything after subject line)
    std::string body_cmd = "cd \"" + maurice_root + "\" && git tag -l --format='%(contents:body)' \"" + latest_tag + "\" 2>/dev/null";
    return exec_command(body_cmd);
}

/**
 * Get the current robot version.
 * - If on main branch and there are tags, returns the latest tag
 * - If in development (not on main), returns dev version using latest tag
 * - Throws runtime_error if no tags exist (this should not happen)
 */
std::string get_robot_version(const std::string& maurice_root) {
    // Get current branch (empty if detached HEAD, e.g. tag checkout)
    std::string branch_cmd = "cd \"" + maurice_root + "\" && git branch --show-current 2>/dev/null";
    std::string current_branch = exec_command(branch_cmd);

    // Check if we're exactly on a tag (works for both branch and detached HEAD)
    std::string exact_cmd = "cd \"" + maurice_root + "\" && git describe --exact-match --tags HEAD 2>/dev/null";
    std::string exact_tag = exec_command(exact_cmd);

    // If on a tag (detached HEAD from tag checkout), return that tag
    if (!exact_tag.empty()) {
        return exact_tag;
    }

    // If not on a branch and not on a tag, we're in an unknown state
    if (current_branch.empty()) {
        throw std::runtime_error("Detached HEAD but not on a tag - checkout a branch or tag");
    }

    // Get all tags sorted by version
    std::string tags_cmd = "cd \"" + maurice_root + "\" && git tag --list --sort=-version:refname 2>/dev/null";
    std::string tags_output = exec_command(tags_cmd);

    if (tags_output.empty()) {
        throw std::runtime_error("No git tags found - repository must have at least one tag");
    }

    // Get first tag (latest)
    std::string latest_tag;
    std::istringstream iss(tags_output);
    std::getline(iss, latest_tag);

    if (latest_tag.empty()) {
        throw std::runtime_error("No git tags found - repository must have at least one tag");
    }

    // Validate tag format (x, x.y, x.y.z, x.y.z.a, etc.) and return dev version
    std::regex version_regex("^(\\d+)(\\.\\d+)*$");
    std::smatch match;
    if (std::regex_match(latest_tag, match, version_regex)) {
        return latest_tag + "-dev";
    } else {
        throw std::runtime_error("Invalid tag format: " + latest_tag + ". Expected format: x[.y[.z[...]]]");
    }
}

/**
 * Get active WiFi SSID using nmcli.
 */
std::string nmcli_get_active_wifi_ssid() {
    std::string result = exec_command("nmcli -t -f active,ssid dev wifi 2>/dev/null | grep '^yes:' | cut -d: -f2");
    return result;
}

/**
 * Check if system updates are available using innate-update --quick-check.
 * Returns true if updates are available, false otherwise.
 */
bool check_update_available(const std::string& maurice_root) {
    // innate-update --quick-check returns 0 if up-to-date, 1 if updates available
    std::string cmd = maurice_root + "/scripts/update/innate-update quick-check >/dev/null 2>&1";
    int result = std::system(cmd.c_str());
    return (WEXITSTATUS(result) != 0);
}

/**
 * Check if a system update is currently running by checking the lock file.
 * Returns true if update is in progress (lock exists and PID is alive).
 */
bool check_update_running() {
    const std::string lock_path = "/tmp/innate-update.lock";
    std::ifstream lock_file(lock_path);
    if (!lock_file.is_open()) {
        return false;
    }
    
    std::string pid_str;
    if (!std::getline(lock_file, pid_str)) {
        return true;  // Malformed lock, assume running to be safe
    }
    
    try {
        pid_t pid = std::stoi(pid_str);
        // Check if process is alive (kill with signal 0 just checks existence)
        if (kill(pid, 0) == 0) {
            return true;  // Process exists
        }
        // Process doesn't exist - stale lock
        std::remove(lock_path.c_str());
        return false;
    } catch (...) {
        return true;  // Malformed, assume running
    }
}

class AppControl : public rclcpp::Node {
public:
    AppControl() : Node("app_control_node") {
        // Load app configuration
        _load_app_config();

        // Cache for WiFi SSID to avoid frequent subprocess calls
        _cached_wifi_ssid = "";
        _last_wifi_check_time = 0.0;
        _wifi_check_interval = 10.0;  // Check WiFi every 10 seconds instead of every 1 second

        // Cache for update check to avoid frequent subprocess calls
        _cached_update_available = false;
        _last_update_check_time = 0.0;
        _update_check_interval = 300.0;  // Check for updates every 5 minutes

        // Declare parameters
        std::string maurice_root = get_maurice_root();
        this->declare_parameter("data_directory", maurice_root + "/data");
        this->declare_parameter("robot_name", "");
        this->declare_parameter("default_hardware_revision", "R6");

        // Subscribe to joystick messages (Vector3)
        joystick_sub_ = this->create_subscription<geometry_msgs::msg::Vector3>(
            "/joystick", 10,
            std::bind(&AppControl::joystick_callback, this, std::placeholders::_1));

        // Subscribe to leader positions messages (Int32MultiArray)
        leader_sub_ = this->create_subscription<std_msgs::msg::Int32MultiArray>(
            "/leader_positions", 10,
            std::bind(&AppControl::leader_positions_callback, this, std::placeholders::_1));

        // Publisher for velocity commands (Twist) on /cmd_vel
        cmd_vel_pub_ = this->create_publisher<geometry_msgs::msg::Twist>("/cmd_vel", 10);

        // Publisher for leader arm commands (Float64MultiArray) on /mars/arm/commands
        cmd_pub_ = this->create_publisher<std_msgs::msg::Float64MultiArray>("/mars/arm/commands", 10);

        // Publisher for robot info
        robot_info_pub_ = this->create_publisher<std_msgs::msg::String>("/robot/info", 10);

        // Timer for publishing robot info
        robot_info_timer_ = this->create_wall_timer(
            1000ms, std::bind(&AppControl::publish_robot_info_callback, this));

        // Service for setting robot name
        set_robot_name_srv_ = this->create_service<maurice_msgs::srv::SetRobotName>(
            "/set_robot_name",
            std::bind(&AppControl::set_robot_name_callback, this,
                      std::placeholders::_1, std::placeholders::_2));

        // Service for triggering system update
        trigger_update_srv_ = this->create_service<maurice_msgs::srv::TriggerUpdate>(
            "/trigger_update",
            std::bind(&AppControl::trigger_update_callback, this,
                      std::placeholders::_1, std::placeholders::_2));

        RCLCPP_INFO(this->get_logger(), "AppControl node started. [C++]");
    }

private:
    std::string get_maurice_root() {
        const char* env_root = std::getenv("INNATE_OS_ROOT");
        if (env_root) {
            return std::string(env_root);
        }
        const char* home = std::getenv("HOME");
        return std::string(home ? home : "/home/jetson1") + "/innate-os";
    }

    /**
     * Load app configuration from YAML config file.
     */
    void _load_app_config() {
        std::string maurice_root = get_maurice_root();
        std::string config_file_path = maurice_root + "/os_config.yaml";

        try {
            if (std::filesystem::exists(config_file_path)) {
                YAML::Node config = YAML::LoadFile(config_file_path);
                if (config["minimum_app_version"]) {
                    app_config_["minimum_app_version"] = config["minimum_app_version"].as<std::string>();
                }
            }
        } catch (const YAML::Exception& e) {
            RCLCPP_WARN(rclcpp::get_logger("app_control"), "Failed to load os_config.yaml: %s", e.what());
            app_config_ = json::object();
        }
    }

    /**
     * Get WiFi SSID with caching to avoid frequent subprocess calls.
     * Only checks for new SSID if the configured interval has passed.
     */
    std::string get_cached_wifi_ssid() {
        double current_time = std::chrono::duration<double>(
            std::chrono::steady_clock::now().time_since_epoch()).count();

        // If we haven't checked recently or don't have a cached value, check now
        if (_cached_wifi_ssid.empty() ||
            (current_time - _last_wifi_check_time) > _wifi_check_interval) {

            std::string new_ssid = nmcli_get_active_wifi_ssid();

            // Only log if the SSID actually changed or this is the first check
            if (new_ssid != _cached_wifi_ssid) {
                if (!new_ssid.empty()) {
                    RCLCPP_INFO(this->get_logger(), "WiFi SSID updated: %s -> %s",
                                _cached_wifi_ssid.c_str(), new_ssid.c_str());
                } else {
                    RCLCPP_WARN(this->get_logger(), "Could not retrieve WiFi SSID");
                }

                _cached_wifi_ssid = new_ssid;
            }
            _last_wifi_check_time = current_time;
        }

        return _cached_wifi_ssid;
    }

    /**
     * Check for system updates with caching to avoid frequent subprocess calls.
     * Only checks if the configured interval has passed.
     * Uses innate-update --quick-check which itself uses a 1-hour cache.
     */
    bool get_cached_update_available() {
        double current_time = std::chrono::duration<double>(
            std::chrono::steady_clock::now().time_since_epoch()).count();

        // Check if we need to refresh the cache
        if (_last_update_check_time == 0.0 ||
            (current_time - _last_update_check_time) > _update_check_interval) {

            bool new_update_available = check_update_available(get_maurice_root());

            // Only log if the status changed
            if (new_update_available != _cached_update_available) {
                if (new_update_available) {
                    RCLCPP_INFO(this->get_logger(), "System updates are available");
                } else {
                    RCLCPP_INFO(this->get_logger(), "System is up to date");
                }
            }

            _cached_update_available = new_update_available;
            _last_update_check_time = current_time;
        }

        return _cached_update_available;
    }

    /**
     * Apply deadband and quadratic curve to input value.
     * - Deadband filters out small inputs
     * - Quadratic curve provides smooth, balanced response
     */
    double apply_curve(double value, double deadband = 0.15) {
        if (std::abs(value) < deadband) {
            return 0.0;
        }

        // Normalize value beyond deadband to range [0, 1] or [-1, 0]
        double sign = (value > 0) ? 1.0 : -1.0;
        double normalized = (std::abs(value) - deadband) / (1.0 - deadband);

        // Apply quadratic curve for balanced progressive response
        double curved = normalized * normalized;

        return sign * curved;
    }

    /**
     * Converts joystick input to velocity commands:
     *   - Linear velocity (x) is scaled from [-1, 1] to [-0.7, 0.7].
     *   - Angular velocity (z) is scaled from [-1, 1] to [-1.5, 1.5].
     *   - Applies deadband of 0.15 (15%) to filter out small inputs.
     *   - Applies cubic curve for smooth, progressive control response.
     */
    void joystick_callback(const geometry_msgs::msg::Vector3::SharedPtr msg) {
        // Apply deadband and curve
        double x = apply_curve(msg->x, 0.15);
        double y = apply_curve(msg->y, 0.15);

        geometry_msgs::msg::Twist twist_msg;
        twist_msg.linear.x = y * 0.5;   // Max forward speed: 0.5 m/s
        twist_msg.angular.z = -x * 1.0; // Max angular speed: 1.0 rad/s

        // Set other components to zero.
        twist_msg.linear.y = 0.0;
        twist_msg.linear.z = 0.0;
        twist_msg.angular.x = 0.0;
        twist_msg.angular.y = 0.0;

        cmd_vel_pub_->publish(twist_msg);
        RCLCPP_INFO(this->get_logger(),
            "Joystick: x=%.2f, y=%.2f -> cmd_vel: linear.x=%.2f, angular.z=%.2f",
            msg->x, msg->y, twist_msg.linear.x, twist_msg.angular.z);
    }

    /**
     * Receives leader positions and converts to radians:
     *   - Conversion: (position - 2048) * (2 * pi / 4096)
     * Then publishes the transformed values as commands.
     *
     * Note: Offset application is currently disabled:
     *   - Offsets: [-1024, 1024, 0, -1024, -1024, 0]
     */
    void leader_positions_callback(const std_msgs::msg::Int32MultiArray::SharedPtr msg) {
        const size_t expected_length = 6;  // Adjust if your leader arm has a different number of joints

        if (msg->data.size() != expected_length) {
            RCLCPP_ERROR(this->get_logger(), "Received %zu positions; expected %zu.",
                         msg->data.size(), expected_length);
            return;
        }

        // Convert positions to radians
        // Offset application (currently disabled)
        // offsets = [-1024, 1024, 0, -1024, -1024, 0]
        // positions_corrected = positions + offsets

        // Convert to radians:
        // positions_rad = (positions_corrected - 2048) * (2 * pi / 4096)
        std_msgs::msg::Float64MultiArray cmd_msg;
        cmd_msg.data.resize(expected_length);

        for (size_t i = 0; i < expected_length; ++i) {
            cmd_msg.data[i] = (static_cast<double>(msg->data[i]) - 2048.0) * (2.0 * M_PI / 4096.0);
        }

        // Publish the transformed command
        cmd_pub_->publish(cmd_msg);
    }

    /**
     * Reads robot_info.json and os_config.json, extracts specified keys, and publishes them as a JSON string.
     * - robot_name: from robot_info.json
     * - minimum_app_version: from os_config.json
     * - wifi_ssid: gets the current WiFi SSID from NetworkManager
     * - version: from git
     * - tag_subject: the subject (first line) of the latest git tag's annotation
     * - tag_body: the body (rest of message) of the latest git tag's annotation
     * - device_id: Bluetooth MAC address from system
     * - update_available: true if system updates are available (from innate-update)
     * Logs errors if file/JSON processing fails or keys are missing.
     * Publishes "{}" if no keys are found or an error occurs.
     */
    void publish_robot_info_callback() {
        std::string data_directory_param = this->get_parameter("data_directory").as_string();
        std::string data_dir = data_directory_param;

        // Expand ~ if present
        if (!data_dir.empty() && data_dir[0] == '~') {
            const char* home = std::getenv("HOME");
            if (home) {
                data_dir = std::string(home) + data_dir.substr(1);
            }
        }

        std::string robot_info_file_path = data_dir + "/robot_info.json";

        json data_to_publish_dict;
        std::string final_json_string_to_publish = "{}";

        try {
            // Ensure data directory exists
            std::filesystem::create_directories(data_dir);

            // Define default robot info values
            std::string default_hw_rev = this->get_parameter("default_hardware_revision").as_string();
            json default_robot_info = {
                {"robot_name", "MARS"},
                {"robot_id", nullptr},
                {"hardware_revision", default_hw_rev},
                {"color_variant", "black"}
            };

            json robot_info;

            // If robot_info.json does not exist, create it with default values
            if (!std::filesystem::exists(robot_info_file_path)) {
                std::ofstream out_file(robot_info_file_path);
                out_file << default_robot_info.dump();
                out_file.close();
                robot_info = default_robot_info;
            } else {
                // Read robot_info from file
                std::ifstream in_file(robot_info_file_path);
                in_file >> robot_info;
                in_file.close();
            }

            // Ensure all default keys exist in the JSON, save if any were missing
            bool updated = false;
            for (auto& [key, default_value] : default_robot_info.items()) {
                if (!robot_info.contains(key)) {
                    robot_info[key] = default_value;
                    updated = true;
                }
            }
            if (updated) {
                std::ofstream out_file(robot_info_file_path);
                out_file << robot_info.dump();
                out_file.close();
            }

            data_to_publish_dict["robot_name"] = robot_info.value("robot_name", "MARS");
            if (robot_info.contains("robot_id") && !robot_info["robot_id"].is_null()) {
                data_to_publish_dict["robot_id"] = robot_info["robot_id"];
            } else {
                data_to_publish_dict["robot_id"] = nullptr;
            }
            data_to_publish_dict["hardware_revision"] = robot_info.value("hardware_revision", default_hw_rev);
            data_to_publish_dict["color_variant"] = robot_info.value("color_variant", "black");

            // Read minimum_app_version from os_config.json
            if (app_config_.contains("minimum_app_version")) {
                data_to_publish_dict["minimum_app_version"] = app_config_["minimum_app_version"];
            }

            // Include WiFi SSID
            std::string wifi_ssid = get_cached_wifi_ssid();
            if (!wifi_ssid.empty()) {
                data_to_publish_dict["wifi_ssid"] = wifi_ssid;
            }

            // Include robot version and tag info
            try {
                std::string maurice_root = get_maurice_root();
                std::string robot_version = get_robot_version(maurice_root);
                data_to_publish_dict["version"] = robot_version;
                
                // Include tag subject and body for the latest version
                std::string tag_subject = get_tag_subject(maurice_root);
                if (!tag_subject.empty()) {
                    data_to_publish_dict["tag_subject"] = tag_subject;
                }
                
                std::string tag_body = get_tag_body(maurice_root);
                if (!tag_body.empty()) {
                    data_to_publish_dict["tag_body"] = tag_body;
                }
            } catch (const std::exception& e) {
                RCLCPP_ERROR(this->get_logger(), "Failed to get robot version: %s", e.what());
            }

            // Include Bluetooth device ID
            std::string device_id = get_bluetooth_device_id();
            if (!device_id.empty()) {
                data_to_publish_dict["device_id"] = device_id;
            }

            // Include update availability status
            data_to_publish_dict["update_available"] = get_cached_update_available();
            
            // Include update running status
            data_to_publish_dict["update_running"] = check_update_running();

            if (!data_to_publish_dict.empty()) {
                final_json_string_to_publish = data_to_publish_dict.dump();
            }

        } catch (const std::exception& e) {
            RCLCPP_ERROR(this->get_logger(), "Error processing robot info: %s", e.what());
            // final_json_string_to_publish remains "{}" as initialized
        }

        std_msgs::msg::String msg;
        msg.data = final_json_string_to_publish;
        robot_info_pub_->publish(msg);
    }

    /**
     * Service callback to change the robot name in robot_info.json.
     */
    void set_robot_name_callback(
        const std::shared_ptr<maurice_msgs::srv::SetRobotName::Request> request,
        std::shared_ptr<maurice_msgs::srv::SetRobotName::Response> response) {

        try {
            std::string data_directory_param = this->get_parameter("data_directory").as_string();
            std::string data_dir = data_directory_param;

            // Expand ~ if present
            if (!data_dir.empty() && data_dir[0] == '~') {
                const char* home = std::getenv("HOME");
                if (home) {
                    data_dir = std::string(home) + data_dir.substr(1);
                }
            }

            std::string robot_info_file_path = data_dir + "/robot_info.json";

            // Load current robot_info
            json robot_info;
            std::ifstream in_file(robot_info_file_path);
            if (in_file.is_open()) {
                in_file >> robot_info;
                in_file.close();
            } else {
                robot_info = json::object();
            }

            // Update robot name
            std::string old_name = robot_info.value("robot_name", "Not set");
            robot_info["robot_name"] = request->robot_name;

            // Save updated robot_info
            std::ofstream out_file(robot_info_file_path);
            out_file << robot_info.dump(2);
            out_file.close();

            response->success = true;
            response->message = "Robot name changed from '" + old_name + "' to '" + request->robot_name + "'";
            RCLCPP_INFO(this->get_logger(), "%s", response->message.c_str());

        } catch (const std::exception& e) {
            response->success = false;
            response->message = std::string("Failed to change robot name: ") + e.what();
            RCLCPP_ERROR(this->get_logger(), "%s", response->message.c_str());
        }
    }

    /**
     * Service callback to trigger a system update.
     * Currently disabled - users should SSH into the robot and run innate-update manually.
     */
    void trigger_update_callback(
        const std::shared_ptr<maurice_msgs::srv::TriggerUpdate::Request> request,
        std::shared_ptr<maurice_msgs::srv::TriggerUpdate::Response> response) {

        (void)request;  // Unused for now
        
        RCLCPP_WARN(this->get_logger(), "Update trigger via service not implemented");
        
        response->success = false;
        response->message = "Not implemented. SSH into the robot and run: innate-update apply";
    }

    // Member variables
    json app_config_;

    // Cache for WiFi SSID to avoid frequent subprocess calls
    std::string _cached_wifi_ssid;
    double _last_wifi_check_time;
    double _wifi_check_interval;

    // Cache for update check to avoid frequent subprocess calls
    bool _cached_update_available;
    double _last_update_check_time;
    double _update_check_interval;

    // Subscribers
    rclcpp::Subscription<geometry_msgs::msg::Vector3>::SharedPtr joystick_sub_;
    rclcpp::Subscription<std_msgs::msg::Int32MultiArray>::SharedPtr leader_sub_;

    // Publishers
    rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr cmd_vel_pub_;
    rclcpp::Publisher<std_msgs::msg::Float64MultiArray>::SharedPtr cmd_pub_;
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr robot_info_pub_;

    // Timer
    rclcpp::TimerBase::SharedPtr robot_info_timer_;

    // Services
    rclcpp::Service<maurice_msgs::srv::SetRobotName>::SharedPtr set_robot_name_srv_;
    rclcpp::Service<maurice_msgs::srv::TriggerUpdate>::SharedPtr trigger_update_srv_;
};

} // namespace maurice_control

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_control::AppControl>();
    try {
        rclcpp::spin(node);
    } catch (const std::exception& e) {
        // Handle exceptions
    }
    rclcpp::shutdown();
    return 0;
}
