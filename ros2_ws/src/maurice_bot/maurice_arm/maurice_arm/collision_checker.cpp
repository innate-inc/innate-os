#include "maurice_arm/collision_checker.hpp"
#include <cmath>
#include <chrono>

namespace maurice_arm {

CollisionChecker::CollisionChecker() 
    : Node("collision_checker") {
    
    RCLCPP_INFO(this->get_logger(), "Collision Checker Node starting...");
    
    // Declare parameters
    this->declare_parameter("publish_markers", true);
    publish_markers_ = this->get_parameter("publish_markers").as_bool();
    
    // Setup collision geometries (hardcoded from URDF)
    setupCollisionGeometries();
    
    // Setup kinematics
    setupKinematics();
    
    // Create ground plane (large box at z=0)
    ground_plane_ = std::make_shared<fcl::Boxd>(100.0, 100.0, 0.01);  // 100m x 100m x 1cm
    ground_ignore_links_ = {"base_link"};  // Ignore base_link collisions with ground
    
    RCLCPP_INFO(this->get_logger(), "Ground plane created, ignoring collisions with base_link");
    
    // Create subscribers and publishers
    joint_state_sub_ = this->create_subscription<sensor_msgs::msg::JointState>(
        "/mars/arm/state", 10,
        std::bind(&CollisionChecker::jointStateCallback, this, std::placeholders::_1));
    
    collision_pub_ = this->create_publisher<std_msgs::msg::Bool>(
        "/mars/arm/collision", 10);
    
    if (publish_markers_) {
        marker_pub_ = this->create_publisher<visualization_msgs::msg::MarkerArray>(
            "/collision_shapes", 10);
    }
    
    RCLCPP_INFO(this->get_logger(), "Collision Checker ready!");
}

void CollisionChecker::setupCollisionGeometries() {
    // base_link: Box 187.8mm x 182mm x 167.6mm
    CollisionGeometry base_geom;
    base_geom.type = CollisionGeometry::Type::BOX;
    base_geom.shape = std::make_shared<fcl::Boxd>(0.1878, 0.182, 0.1676);
    base_geom.offset = Eigen::Vector3d(-0.0587, 0, 0.0838);
    base_geom.rpy = Eigen::Vector3d(0, 0, 0);
    base_geom.link_name = "base_link";
    collision_geometries_["base_link"] = base_geom;
    
    // link2: Box 20mm x 54mm x 138.5mm (length along link direction)
    CollisionGeometry link2_geom;
    link2_geom.type = CollisionGeometry::Type::BOX;
    link2_geom.shape = std::make_shared<fcl::Boxd>(0.020, 0.054, 0.1385);
    link2_geom.offset = Eigen::Vector3d(0, 0, 0.06925);
    link2_geom.rpy = Eigen::Vector3d(0, 0, 0);
    link2_geom.link_name = "link2";
    collision_geometries_["link2"] = link2_geom;
    
    // link3: Cylinder length 118mm, radius 23mm
    CollisionGeometry link3_geom;
    link3_geom.type = CollisionGeometry::Type::CYLINDER;
    link3_geom.shape = std::make_shared<fcl::Cylinderd>(0.023, 0.118);
    link3_geom.offset = Eigen::Vector3d(0.059, 0, 0);
    link3_geom.rpy = Eigen::Vector3d(0, 1.5708, 0);
    link3_geom.link_name = "link3";
    collision_geometries_["link3"] = link3_geom;
    
    // link61: Box 48mm x 16mm x 38.9mm
    CollisionGeometry link61_geom;
    link61_geom.type = CollisionGeometry::Type::BOX;
    link61_geom.shape = std::make_shared<fcl::Boxd>(0.048, 0.016, 0.0389);
    link61_geom.offset = Eigen::Vector3d(0.024, 0, 0);
    link61_geom.rpy = Eigen::Vector3d(0, 0, 0);
    link61_geom.link_name = "link61";
    collision_geometries_["link61"] = link61_geom;
    
    // link62: Box 48mm x 16mm x 38.9mm
    CollisionGeometry link62_geom;
    link62_geom.type = CollisionGeometry::Type::BOX;
    link62_geom.shape = std::make_shared<fcl::Boxd>(0.048, 0.016, 0.0389);
    link62_geom.offset = Eigen::Vector3d(0.024, 0, 0);
    link62_geom.rpy = Eigen::Vector3d(0, 0, 0);
    link62_geom.link_name = "link62";
    collision_geometries_["link62"] = link62_geom;
    
    RCLCPP_INFO(this->get_logger(), "Loaded %zu collision geometries", 
                collision_geometries_.size());
    
    // Define adjacent links to IGNORE in collision checking
    // Only ignore: link2<->link3 (adjacent in chain) and link61<->link62 (gripper fingers)
    adjacent_links_ = {
        {"link2", "link3"},     // Adjacent in kinematic chain
        {"link61", "link62"}    // Gripper fingers (always close/touching)
    };
    
    RCLCPP_INFO(this->get_logger(), "Ignoring collisions between %zu adjacent pairs", 
                adjacent_links_.size());
}

void CollisionChecker::setupKinematics() {
    // Hardcode joint information from URDF
    
    // joint1: base_link -> link1
    joints_["joint1"] = {
        Eigen::Vector3d(0.086, -0.05285, 0.04025),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 0, 1),
        "base_link", "link1"
    };
    
    // joint2: link1 -> link2
    joints_["joint2"] = {
        Eigen::Vector3d(0, 0, 0.04225),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 1, 0),
        "link1", "link2"
    };
    
    // joint3: link2 -> link3
    joints_["joint3"] = {
        Eigen::Vector3d(0.02825, 0, 0.12125),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 1, 0),
        "link2", "link3"
    };
    
    // joint4: link3 -> link4
    joints_["joint4"] = {
        Eigen::Vector3d(0.1375, 0, -0.0045),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 1, 0),
        "link3", "link4"
    };
    
    // joint5: link4 -> link5
    joints_["joint5"] = {
        Eigen::Vector3d(0.019, 0, 0),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(1, 0, 0),
        "link4", "link5"
    };
    
    // joint6: link5 -> link61
    joints_["joint6"] = {
        Eigen::Vector3d(0.044, -0.0091, 0),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 0, 1),
        "link5", "link61"
    };
    
    // joint6M: link5 -> link62 (mirrored)
    joints_["joint6M"] = {
        Eigen::Vector3d(0.044, 0.0091, 0),
        Eigen::Vector3d(0, 0, 0),
        Eigen::Vector3d(0, 0, 1),
        "link5", "link62"
    };
    
    RCLCPP_INFO(this->get_logger(), "Configured %zu joints for forward kinematics", 
                joints_.size());
}

Eigen::Isometry3d CollisionChecker::createTransform(
    const Eigen::Vector3d& xyz, 
    const Eigen::Vector3d& rpy) {
    
    Eigen::Isometry3d transform = Eigen::Isometry3d::Identity();
    
    // Create rotation matrix from RPY (Roll-Pitch-Yaw)
    Eigen::AngleAxisd rollAngle(rpy[0], Eigen::Vector3d::UnitX());
    Eigen::AngleAxisd pitchAngle(rpy[1], Eigen::Vector3d::UnitY());
    Eigen::AngleAxisd yawAngle(rpy[2], Eigen::Vector3d::UnitZ());
    
    Eigen::Quaterniond q = yawAngle * pitchAngle * rollAngle;
    transform.rotate(q);
    transform.translation() = xyz;
    
    return transform;
}

std::map<std::string, Eigen::Isometry3d> CollisionChecker::computeForwardKinematics(
    const std::vector<double>& joint_positions) {
    
    std::map<std::string, Eigen::Isometry3d> transforms;
    
    // Base link is at origin
    transforms["base_link"] = Eigen::Isometry3d::Identity();
    
    // Compute transforms for each link
    std::vector<std::string> joint_names = {"joint1", "joint2", "joint3", "joint4", "joint5", "joint6"};
    
    for (size_t i = 0; i < joint_names.size() && i < joint_positions.size(); ++i) {
        const std::string& joint_name = joint_names[i];
        const auto& joint = joints_[joint_name];
        
        // Get parent transform
        Eigen::Isometry3d parent_transform = transforms[joint.parent_link];
        
        // Joint origin transform
        Eigen::Isometry3d joint_origin = createTransform(joint.xyz, joint.rpy);
        
        // Joint rotation (flip direction for joint6)
        Eigen::Isometry3d joint_rotation = Eigen::Isometry3d::Identity();
        double joint_angle = joint_positions[i];
        if (i == 5) {  // joint6 (index 5)
            joint_angle = -joint_angle;  // Flip direction
        }
        Eigen::AngleAxisd rotation(joint_angle, joint.axis);
        joint_rotation.rotate(rotation);
        
        // Child link transform
        transforms[joint.child_link] = parent_transform * joint_origin * joint_rotation;
    }
    
    // Handle mirrored gripper (joint6M mirrors joint6 with flipped direction)
    if (joint_positions.size() >= 6) {
        const auto& joint6M = joints_["joint6M"];
        Eigen::Isometry3d parent_transform = transforms["link5"];
        Eigen::Isometry3d joint_origin = createTransform(joint6M.xyz, joint6M.rpy);
        Eigen::Isometry3d joint_rotation = Eigen::Isometry3d::Identity();
        Eigen::AngleAxisd rotation(joint_positions[5], joint6M.axis);  // Same direction as flipped joint6
        joint_rotation.rotate(rotation);
        transforms["link62"] = parent_transform * joint_origin * joint_rotation;
    }
    
    return transforms;
}

bool CollisionChecker::areAdjacent(const std::string& link1, const std::string& link2) {
    for (const auto& pair : adjacent_links_) {
        if ((pair.first == link1 && pair.second == link2) ||
            (pair.first == link2 && pair.second == link1)) {
            return true;
        }
    }
    return false;
}

bool CollisionChecker::checkCollisions(
    const std::map<std::string, Eigen::Isometry3d>& transforms) {
    
    bool collision_detected = false;
    
    // Create FCL collision objects with current transforms
    std::vector<std::pair<std::string, fcl::CollisionObjectd>> fcl_objects;
    
    for (const auto& [link_name, geom] : collision_geometries_) {
        if (transforms.find(link_name) != transforms.end()) {
            // Get link transform
            Eigen::Isometry3d link_transform = transforms.at(link_name);
            
            // Apply collision geometry offset
            Eigen::Isometry3d offset_transform = createTransform(geom.offset, geom.rpy);
            Eigen::Isometry3d final_transform = link_transform * offset_transform;
            
            // Create FCL collision object
            fcl::CollisionObjectd coll_obj(geom.shape, final_transform);
            fcl_objects.emplace_back(link_name, coll_obj);
        }
    }
    
    // Check all pairs for collision
    for (size_t i = 0; i < fcl_objects.size(); ++i) {
        for (size_t j = i + 1; j < fcl_objects.size(); ++j) {
            const std::string& link1 = fcl_objects[i].first;
            const std::string& link2 = fcl_objects[j].first;
            
            // Skip adjacent links
            if (areAdjacent(link1, link2)) {
                continue;
            }
            
            // Perform collision check
            fcl::CollisionRequestd request;
            fcl::CollisionResultd result;
            
            fcl::collide(&fcl_objects[i].second, &fcl_objects[j].second, request, result);
            
            if (result.isCollision()) {
                collision_detected = true;
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
                    "Self-collision detected: %s <-> %s", link1.c_str(), link2.c_str());
            }
        }
    }
    
    return collision_detected;
}

bool CollisionChecker::checkGroundCollisions(
    const std::map<std::string, Eigen::Isometry3d>& transforms) {
    
    bool collision_detected = false;
    
    // Create ground plane collision object at z=0
    Eigen::Isometry3d ground_transform = Eigen::Isometry3d::Identity();
    ground_transform.translation() = Eigen::Vector3d(0, 0, -0.005);  // Ground at z=-0.005 (half thickness)
    fcl::CollisionObjectd ground_obj(ground_plane_, ground_transform);
    
    // Check each link against ground
    for (const auto& [link_name, geom] : collision_geometries_) {
        // Skip links in the ignore list
        if (std::find(ground_ignore_links_.begin(), ground_ignore_links_.end(), link_name) 
            != ground_ignore_links_.end()) {
            continue;
        }
        
        if (transforms.find(link_name) != transforms.end()) {
            // Get link transform
            Eigen::Isometry3d link_transform = transforms.at(link_name);
            
            // Apply collision geometry offset
            Eigen::Isometry3d offset_transform = createTransform(geom.offset, geom.rpy);
            Eigen::Isometry3d final_transform = link_transform * offset_transform;
            
            // Create FCL collision object
            fcl::CollisionObjectd link_obj(geom.shape, final_transform);
            
            // Perform collision check with ground
            fcl::CollisionRequestd request;
            fcl::CollisionResultd result;
            
            fcl::collide(&link_obj, &ground_obj, request, result);
            
            if (result.isCollision()) {
                collision_detected = true;
                RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
                    "Ground collision detected: %s", link_name.c_str());
            }
        }
    }
    
    return collision_detected;
}

void CollisionChecker::jointStateCallback(
    const sensor_msgs::msg::JointState::SharedPtr msg) {
    
    auto callback_start = std::chrono::high_resolution_clock::now();
    
    // Compute forward kinematics
    auto fk_start = std::chrono::high_resolution_clock::now();
    auto transforms = computeForwardKinematics(msg->position);
    auto fk_end = std::chrono::high_resolution_clock::now();
    auto fk_duration = std::chrono::duration_cast<std::chrono::microseconds>(fk_end - fk_start);
    
    // Check for self-collisions
    auto self_col_start = std::chrono::high_resolution_clock::now();
    bool self_collision = checkCollisions(transforms);
    auto self_col_end = std::chrono::high_resolution_clock::now();
    auto self_col_duration = std::chrono::duration_cast<std::chrono::microseconds>(self_col_end - self_col_start);
    
    // Check for ground collisions
    auto ground_col_start = std::chrono::high_resolution_clock::now();
    bool ground_collision = checkGroundCollisions(transforms);
    auto ground_col_end = std::chrono::high_resolution_clock::now();
    auto ground_col_duration = std::chrono::duration_cast<std::chrono::microseconds>(ground_col_end - ground_col_start);
    
    // Combine collision results
    bool collision = self_collision || ground_collision;
    
    // Publish collision status
    auto collision_msg = std_msgs::msg::Bool();
    collision_msg.data = collision;
    collision_pub_->publish(collision_msg);
    
    // Publish visualization markers
    auto viz_start = std::chrono::high_resolution_clock::now();
    if (publish_markers_) {
        publishCollisionMarkers(transforms, collision);
    }
    auto viz_end = std::chrono::high_resolution_clock::now();
    auto viz_duration = std::chrono::duration_cast<std::chrono::microseconds>(viz_end - viz_start);
    
    auto callback_end = std::chrono::high_resolution_clock::now();
    auto total_duration = std::chrono::duration_cast<std::chrono::microseconds>(callback_end - callback_start);
    
    // Log profiling information (throttled to once per second)
    RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
        "⏱️  Collision Checker Profile:\n"
        "   Forward Kinematics: %ld μs\n"
        "   Self-Collision:     %ld μs (8 pairs)\n"
        "   Ground Collision:   %ld μs (4 links)\n"
        "   Visualization:      %ld μs\n"
        "   ═══════════════════════════════\n"
        "   TOTAL:              %ld μs (%.3f ms)\n"
        "   Max Frequency:      %.1f Hz",
        fk_duration.count(),
        self_col_duration.count(),
        ground_col_duration.count(),
        viz_duration.count(),
        total_duration.count(),
        total_duration.count() / 1000.0,
        1000000.0 / total_duration.count());
}

void CollisionChecker::publishCollisionMarkers(
    const std::map<std::string, Eigen::Isometry3d>& transforms,
    bool collision_detected) {
    
    auto marker_array = visualization_msgs::msg::MarkerArray();
    int id = 0;
    
    // Publish collision shape markers
    for (const auto& [link_name, geom] : collision_geometries_) {
        if (transforms.find(link_name) == transforms.end()) continue;
        
        auto marker = visualization_msgs::msg::Marker();
        marker.header.frame_id = "base_link";
        marker.header.stamp = this->now();
        marker.ns = "collision_shapes";
        marker.id = id++;
        marker.action = visualization_msgs::msg::Marker::ADD;
        
        // Get transform
        Eigen::Isometry3d link_transform = transforms.at(link_name);
        Eigen::Isometry3d offset_transform = createTransform(geom.offset, geom.rpy);
        Eigen::Isometry3d final_transform = link_transform * offset_transform;
        
        // Set pose
        marker.pose.position.x = final_transform.translation().x();
        marker.pose.position.y = final_transform.translation().y();
        marker.pose.position.z = final_transform.translation().z();
        
        Eigen::Quaterniond q(final_transform.rotation());
        marker.pose.orientation.x = q.x();
        marker.pose.orientation.y = q.y();
        marker.pose.orientation.z = q.z();
        marker.pose.orientation.w = q.w();
        
        // Set shape
        if (geom.type == CollisionGeometry::Type::BOX) {
            marker.type = visualization_msgs::msg::Marker::CUBE;
            auto box = std::static_pointer_cast<fcl::Boxd>(geom.shape);
            marker.scale.x = box->side[0];
            marker.scale.y = box->side[1];
            marker.scale.z = box->side[2];
        } else if (geom.type == CollisionGeometry::Type::CYLINDER) {
            marker.type = visualization_msgs::msg::Marker::CYLINDER;
            auto cyl = std::static_pointer_cast<fcl::Cylinderd>(geom.shape);
            marker.scale.x = cyl->radius * 2;
            marker.scale.y = cyl->radius * 2;
            marker.scale.z = cyl->lz;
        }
        
        // Set color (green if no collision, red if collision)
        marker.color.r = collision_detected ? 1.0 : 0.0;
        marker.color.g = collision_detected ? 0.0 : 1.0;
        marker.color.b = 0.0;
        marker.color.a = 0.5;
        
        marker_array.markers.push_back(marker);
    }
    
    // Publish ground plane marker
    auto ground_marker = visualization_msgs::msg::Marker();
    ground_marker.header.frame_id = "base_link";
    ground_marker.header.stamp = this->now();
    ground_marker.ns = "collision_shapes";
    ground_marker.id = id++;
    ground_marker.action = visualization_msgs::msg::Marker::ADD;
    ground_marker.type = visualization_msgs::msg::Marker::CUBE;
    
    ground_marker.pose.position.x = 0.0;
    ground_marker.pose.position.y = 0.0;
    ground_marker.pose.position.z = -0.005;
    ground_marker.pose.orientation.w = 1.0;
    
    ground_marker.scale.x = 2.0;  // 2m x 2m for visualization
    ground_marker.scale.y = 2.0;
    ground_marker.scale.z = 0.01;
    
    ground_marker.color.r = 0.5;
    ground_marker.color.g = 0.5;
    ground_marker.color.b = 0.5;
    ground_marker.color.a = 0.3;
    
    marker_array.markers.push_back(ground_marker);
    
    marker_pub_->publish(marker_array);
}

} // namespace maurice_arm

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::CollisionChecker>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}

