#ifndef MAURICE_ARM_COLLISION_CHECKER_HPP
#define MAURICE_ARM_COLLISION_CHECKER_HPP

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/joint_state.hpp>
#include <std_msgs/msg/bool.hpp>
#include <visualization_msgs/msg/marker_array.hpp>
// Include only specific FCL headers we need (avoids octomap dependency issues)
#include <fcl/geometry/collision_geometry.h>
#include <fcl/geometry/shape/box.h>
#include <fcl/geometry/shape/cylinder.h>
#include <fcl/narrowphase/collision.h>
#include <fcl/narrowphase/collision_object.h>
#include <fcl/narrowphase/distance.h>
#include <Eigen/Dense>
#include <Eigen/Geometry>
#include <memory>
#include <vector>
#include <map>
#include <string>

namespace maurice_arm {

struct CollisionGeometry {
    enum class Type { BOX, CYLINDER, SPHERE };
    
    Type type;
    std::shared_ptr<fcl::CollisionGeometryd> shape;
    Eigen::Vector3d offset;
    Eigen::Vector3d rpy;
    std::string link_name;
};

// Result structure for collision/distance queries
struct CollisionCheckResult {
    bool in_collision;
    double min_clearance;  // Minimum distance to any obstacle (negative if penetrating)
    std::string closest_pair;  // Description of closest link pair
    
    // Per-link clearance information for selective joint scaling
    std::map<std::string, double> link_clearances;  // Minimum clearance for each link
    std::map<std::string, std::string> link_closest_to;  // What each link is closest to
    
    CollisionCheckResult() 
        : in_collision(false), 
          min_clearance(std::numeric_limits<double>::max()),
          closest_pair("") {}
};

// Core collision checking library (no ROS dependencies)
class CollisionCheckerCore {
public:
    CollisionCheckerCore();
    ~CollisionCheckerCore() = default;
    
    // Main API for collision checking
    CollisionCheckResult checkConfiguration(const std::vector<double>& joint_positions);
    
    // Forward kinematics
    std::map<std::string, Eigen::Isometry3d> computeForwardKinematics(
        const std::vector<double>& joint_positions);
    
    Eigen::Isometry3d createTransform(
        const Eigen::Vector3d& xyz, 
        const Eigen::Vector3d& rpy);
    
    // Accessors for visualization
    const std::map<std::string, CollisionGeometry>& getCollisionGeometries() const {
        return collision_geometries_;
    }

private:
    void setupCollisionGeometries();
    void setupKinematics();
    
    // Collision checking internals
    bool checkCollisions(
        const std::map<std::string, Eigen::Isometry3d>& transforms,
        double& min_clearance,
        std::string& closest_pair,
        std::map<std::string, double>& link_clearances,
        std::map<std::string, std::string>& link_closest_to);
    
    bool checkGroundCollisions(
        const std::map<std::string, Eigen::Isometry3d>& transforms,
        double& min_clearance,
        std::string& closest_link,
        std::map<std::string, double>& link_clearances,
        std::map<std::string, std::string>& link_closest_to);
    
    bool areAdjacent(const std::string& link1, const std::string& link2);
    
    // Collision data
    std::map<std::string, CollisionGeometry> collision_geometries_;
    std::vector<std::pair<std::string, std::string>> adjacent_links_;
    std::shared_ptr<fcl::CollisionGeometryd> ground_plane_;
    std::vector<std::string> ground_ignore_links_;
    
    // Kinematics data (from URDF joint origins)
    struct JointInfo {
        Eigen::Vector3d xyz;
        Eigen::Vector3d rpy;
        Eigen::Vector3d axis;
        std::string parent_link;
        std::string child_link;
    };
    std::map<std::string, JointInfo> joints_;
};

// ROS2 Node wrapper around CollisionCheckerCore
class CollisionChecker : public rclcpp::Node {
public:
    CollisionChecker();
    ~CollisionChecker() = default;

private:
    void jointStateCallback(const sensor_msgs::msg::JointState::SharedPtr msg);
    
    // Visualization
    void publishCollisionMarkers(
        const std::map<std::string, Eigen::Isometry3d>& transforms,
        bool collision_detected);
    
    // ROS2 interfaces
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr collision_pub_;
    rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr marker_pub_;
    
    // Core collision checker
    std::unique_ptr<CollisionCheckerCore> collision_checker_core_;
    
    // Parameters
    bool publish_markers_;
};

} // namespace maurice_arm

#endif // MAURICE_ARM_COLLISION_CHECKER_HPP

