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

class CollisionChecker : public rclcpp::Node {
public:
    CollisionChecker();
    ~CollisionChecker() = default;

private:
    void jointStateCallback(const sensor_msgs::msg::JointState::SharedPtr msg);
    
    void setupCollisionGeometries();
    void setupKinematics();
    
    // Forward kinematics
    std::map<std::string, Eigen::Isometry3d> computeForwardKinematics(
        const std::vector<double>& joint_positions);
    
    Eigen::Isometry3d createTransform(
        const Eigen::Vector3d& xyz, 
        const Eigen::Vector3d& rpy);
    
    // Collision checking
    bool checkCollisions(const std::map<std::string, Eigen::Isometry3d>& transforms);
    bool checkGroundCollisions(const std::map<std::string, Eigen::Isometry3d>& transforms);
    bool areAdjacent(const std::string& link1, const std::string& link2);
    
    // Visualization
    void publishCollisionMarkers(
        const std::map<std::string, Eigen::Isometry3d>& transforms,
        bool collision_detected);
    
    // ROS2 interfaces
    rclcpp::Subscription<sensor_msgs::msg::JointState>::SharedPtr joint_state_sub_;
    rclcpp::Publisher<std_msgs::msg::Bool>::SharedPtr collision_pub_;
    rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr marker_pub_;
    
    // Collision data
    std::map<std::string, CollisionGeometry> collision_geometries_;
    std::vector<std::pair<std::string, std::string>> adjacent_links_;
    std::shared_ptr<fcl::CollisionGeometryd> ground_plane_;
    std::vector<std::string> ground_ignore_links_;  // Links to ignore for ground collision
    
    // Kinematics data (from URDF joint origins)
    struct JointInfo {
        Eigen::Vector3d xyz;
        Eigen::Vector3d rpy;
        Eigen::Vector3d axis;
        std::string parent_link;
        std::string child_link;
    };
    std::map<std::string, JointInfo> joints_;
    
    // Parameters
    bool publish_markers_;
};

} // namespace maurice_arm

#endif // MAURICE_ARM_COLLISION_CHECKER_HPP

