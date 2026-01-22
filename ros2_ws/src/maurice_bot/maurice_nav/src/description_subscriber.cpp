#include <memory>
#include <string>
#include <chrono>
#include <array>
#include <algorithm>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "geometry_msgs/msg/transform_stamped.hpp"
#include "geometry_msgs/msg/pose_stamped.hpp"
#include "geometry_msgs/msg/polygon_stamped.hpp"
#include "geometry_msgs/msg/polygon.hpp"
#include "geometry_msgs/msg/point.hpp"
#include "tf2_msgs/msg/tf_message.hpp"
#include "std_msgs/msg/string.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"
#include "visualization_msgs/msg/marker.hpp"
#include "visualization_msgs/msg/marker_array.hpp"
#include "urdf/model.h"

using namespace std::chrono_literals;

/**
 * DescriptionSubscriber Node
 * 
 * Subscribes to:
 * - /robot_description (via ROS parameter)
 * - /tf (dynamic transforms)
 * - /tf_static (static transforms)
 */
class DescriptionSubscriber : public rclcpp::Node
{
public:
  explicit DescriptionSubscriber(const rclcpp::NodeOptions & options = rclcpp::NodeOptions())
  : rclcpp::Node("description_subscriber", options),
    tf_buffer_(this->get_clock()),
    tf_listener_(tf_buffer_)
  {
    RCLCPP_INFO(this->get_logger(), "DescriptionSubscriber node initialized");

    this->declare_parameter<double>("padding", 0.03);
    this->declare_parameter<double>("update_frequency", 5.0);
    this->declare_parameter<bool>("debug", false);

    const double update_freq = this->get_parameter("update_frequency").as_double();
    const bool debug_mode = this->get_parameter("debug").as_bool();

    // tf2_ros::TransformListener automatically subscribes to /tf and /tf_static
    // and maintains them in tf_buffer_

    // Create publisher for footprint (Polygon type for costmaps)
    footprint_publisher_ = this->create_publisher<geometry_msgs::msg::Polygon>("/footprint", 10);

    // Only create debug publishers if debug mode is enabled
    if (debug_mode) {
      collision_points_publisher_ = this->create_publisher<visualization_msgs::msg::MarkerArray>("/collision_points", 10);
      collision_boxes_publisher_ = this->create_publisher<visualization_msgs::msg::MarkerArray>("/collision_boxes", 10);
      robot_hull_publisher_ = this->create_publisher<visualization_msgs::msg::Marker>("/robot_convex_hull", 10);
    }

    // Create a timer based on update_frequency param
    const auto period = std::chrono::duration<double>(1.0 / update_freq);
    timer_ = this->create_wall_timer(
      std::chrono::duration_cast<std::chrono::nanoseconds>(period),
      std::bind(&DescriptionSubscriber::timer_callback, this));

    // Get robot_description from parameter server
    get_robot_description();
  }

private:
  static double sign0(double v)
  {
    if (v > 0.0) {
      return 1.0;
    }
    if (v < 0.0) {
      return -1.0;
    }
    return 0.0;
  }

  static void padFootprint(std::vector<geometry_msgs::msg::Point> & footprint, double padding)
  {
    // pad footprint in place
    for (unsigned int i = 0; i < footprint.size(); i++) {
      geometry_msgs::msg::Point & pt = footprint[i];
      pt.x += sign0(pt.x) * padding;
      pt.y += sign0(pt.y) * padding;
    }
  }

  struct Point2
  {
    double x;
    double y;

    bool operator<(const Point2 & other) const
    {
      if (x < other.x) {
        return true;
      }
      if (x > other.x) {
        return false;
      }
      return y < other.y;
    }

    bool operator==(const Point2 & other) const
    {
      return x == other.x && y == other.y;
    }
  };

  static double cross(const Point2 & o, const Point2 & a, const Point2 & b)
  {
    return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  }

  static std::vector<Point2> convex_hull(std::vector<Point2> pts)
  {
    if (pts.size() <= 1) {
      return pts;
    }

    std::sort(pts.begin(), pts.end());
    pts.erase(std::unique(pts.begin(), pts.end()), pts.end());

    if (pts.size() <= 2) {
      return pts;
    }

    std::vector<Point2> lower;
    lower.reserve(pts.size());
    for (const auto & p : pts) {
      while (lower.size() >= 2 && cross(lower[lower.size() - 2], lower[lower.size() - 1], p) <= 0.0) {
        lower.pop_back();
      }
      lower.push_back(p);
    }

    std::vector<Point2> upper;
    upper.reserve(pts.size());
    for (auto it = pts.rbegin(); it != pts.rend(); ++it) {
      const auto & p = *it;
      while (upper.size() >= 2 && cross(upper[upper.size() - 2], upper[upper.size() - 1], p) <= 0.0) {
        upper.pop_back();
      }
      upper.push_back(p);
    }

    lower.pop_back();
    upper.pop_back();
    lower.insert(lower.end(), upper.begin(), upper.end());
    return lower;
  }

  tf2_ros::Buffer tf_buffer_;
  tf2_ros::TransformListener tf_listener_;
  rclcpp::Publisher<geometry_msgs::msg::Polygon>::SharedPtr footprint_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr collision_points_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr collision_boxes_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::Marker>::SharedPtr robot_hull_publisher_;
  rclcpp::TimerBase::SharedPtr timer_;
  std::string robot_description_;
  std::shared_ptr<urdf::Model> urdf_model_;

  /**
   * Retrieve robot_description from parameter server
   */
  void get_robot_description()
  {
    try {
      auto parameters_client = std::make_shared<rclcpp::SyncParametersClient>(this, "robot_state_publisher");
      
      while (!parameters_client->wait_for_service(std::chrono::seconds(1))) {
        if (!rclcpp::ok()) {
          RCLCPP_WARN(this->get_logger(), "Interrupted while waiting for parameter server");
          return;
        }
        RCLCPP_INFO(this->get_logger(), "Waiting for parameter server...");
      }

      auto params = parameters_client->get_parameters({"robot_description"});
      if (!params.empty()) {
        robot_description_ = params[0].as_string();
        RCLCPP_INFO(this->get_logger(), "Successfully retrieved robot_description (length: %zu bytes)",
                    robot_description_.length());
        
        // Parse URDF
        parse_urdf();
      } else {
        RCLCPP_WARN(this->get_logger(), "robot_description parameter not found");
      }
    } catch (const std::exception & e) {
      RCLCPP_ERROR(this->get_logger(), "Failed to get robot_description: %s", e.what());
    }
  }

  /**
   * Parse URDF and extract robot information
   */
  void parse_urdf()
  {
    try {
      urdf_model_ = std::make_shared<urdf::Model>();
      if (!urdf_model_->initString(robot_description_)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to parse URDF");
        urdf_model_ = nullptr;
        return;
      }
      
      RCLCPP_INFO(this->get_logger(), "Successfully parsed URDF for robot: %s", urdf_model_->getName().c_str());
      
      // Log links information
      RCLCPP_INFO(this->get_logger(), "Number of links: %zu", urdf_model_->links_.size());
      for (const auto & link_pair : urdf_model_->links_) {
        const auto & link = link_pair.second;
        RCLCPP_DEBUG(this->get_logger(), "  Link: %s", link->name.c_str());
        
        if (link->inertial) {
          RCLCPP_DEBUG(this->get_logger(), "    Mass: %f kg", link->inertial->mass);
          RCLCPP_DEBUG(this->get_logger(), "    Inertia: Ixx=%f, Iyy=%f, Izz=%f",
                       link->inertial->ixx, link->inertial->iyy, link->inertial->izz);
        }
        
        // Log collision geometry
        if (!link->collision_array.empty()) {
          RCLCPP_INFO(this->get_logger(), "  Link '%s' - Collision objects: %zu", 
                      link->name.c_str(), link->collision_array.size());
          
          for (size_t i = 0; i < link->collision_array.size(); ++i) {
            const auto & collision = link->collision_array[i];
            RCLCPP_INFO(this->get_logger(), "    Collision [%zu]: name=%s", i, collision->name.c_str());
            
            RCLCPP_INFO(this->get_logger(), "      Origin: xyz=(%f, %f, %f)", 
                        collision->origin.position.x, 
                        collision->origin.position.y, 
                        collision->origin.position.z);
            
            if (collision->geometry) {
              const auto & geom = collision->geometry;
              
              if (geom->type == urdf::Geometry::BOX) {
                auto box = std::dynamic_pointer_cast<urdf::Box>(geom);
                if (box) {
                  RCLCPP_INFO(this->get_logger(), "      Geometry: BOX - size=(%f, %f, %f)",
                              box->dim.x, box->dim.y, box->dim.z);

                }
              } else if (geom->type == urdf::Geometry::CYLINDER) {
                auto cyl = std::dynamic_pointer_cast<urdf::Cylinder>(geom);
                if (cyl) {
                  RCLCPP_INFO(this->get_logger(), "      Geometry: CYLINDER - radius=%f, length=%f",
                              cyl->radius, cyl->length);
                }
              } else if (geom->type == urdf::Geometry::SPHERE) {
                auto sphere = std::dynamic_pointer_cast<urdf::Sphere>(geom);
                if (sphere) {
                  RCLCPP_INFO(this->get_logger(), "      Geometry: SPHERE - radius=%f", sphere->radius);
                }
              } else if (geom->type == urdf::Geometry::MESH) {
                auto mesh = std::dynamic_pointer_cast<urdf::Mesh>(geom);
                if (mesh) {
                  RCLCPP_INFO(this->get_logger(), "      Geometry: MESH - filename=%s, scale=(%f, %f, %f)",
                              mesh->filename.c_str(), mesh->scale.x, mesh->scale.y, mesh->scale.z);
                }
              }
            }
          }
        }
        
        if (!link->visual_array.empty()) {
          RCLCPP_DEBUG(this->get_logger(), "    Visual objects: %zu", link->visual_array.size());
        }
      }
      
      // Log joints information
      RCLCPP_INFO(this->get_logger(), "Number of joints: %zu", urdf_model_->joints_.size());
      for (const auto & joint_pair : urdf_model_->joints_) {
        const auto & joint = joint_pair.second;
        RCLCPP_DEBUG(this->get_logger(), "  Joint: %s (%s) - %s -> %s",
                     joint->name.c_str(),
                     joint->type == urdf::Joint::FIXED ? "FIXED" :
                     joint->type == urdf::Joint::REVOLUTE ? "REVOLUTE" :
                     joint->type == urdf::Joint::CONTINUOUS ? "CONTINUOUS" :
                     joint->type == urdf::Joint::PRISMATIC ? "PRISMATIC" :
                     joint->type == urdf::Joint::PLANAR ? "PLANAR" : "UNKNOWN",
                     joint->parent_link_name.c_str(),
                     joint->child_link_name.c_str());
      }
      
    } catch (const std::exception & e) {
      RCLCPP_ERROR(this->get_logger(), "Failed to parse URDF: %s", e.what());
    }
  }

  /**
   * Timer callback to publish footprint every 2 seconds
   */
  void timer_callback()
  {
    if (!urdf_model_) {
      RCLCPP_WARN_ONCE(this->get_logger(), "URDF model not loaded, skipping footprint publication");
      return;
    }

    // Process each link's collision geometry and publish outputs
    process_collision_boxes();
  }

  /**
   * Process collision boxes from each link and transform to base_link frame
   */
  void process_collision_boxes()
  {
    const bool debug_mode = this->get_parameter("debug").as_bool();
    visualization_msgs::msg::MarkerArray points_array;
    visualization_msgs::msg::MarkerArray boxes_array;
    int marker_id = 0;

    std::vector<Point2> all_points_xy;
    all_points_xy.reserve(512);

    for (const auto & link_pair : urdf_model_->links_) {
      const auto & link = link_pair.second;
      const std::string & link_name = link->name;

      if (link->collision_array.empty()) {
        continue;
      }

      for (size_t i = 0; i < link->collision_array.size(); ++i) {
        const auto & collision = link->collision_array[i];
        
        if (!collision->geometry) {
          continue;
        }

        const auto & geom = collision->geometry;

        // Only process BOX geometry
        if (geom->type != urdf::Geometry::BOX) {
          const char* type_str = 
            geom->type == urdf::Geometry::CYLINDER ? "CYLINDER" :
            geom->type == urdf::Geometry::SPHERE ? "SPHERE" :
            geom->type == urdf::Geometry::MESH ? "MESH" : "UNKNOWN";
          RCLCPP_DEBUG(this->get_logger(), "Link '%s' collision[%zu]: Ignoring geometry type %s",
                      link_name.c_str(), i, type_str);
          continue;
        }

        auto box = std::dynamic_pointer_cast<urdf::Box>(geom);
        if (!box) {
          continue;
        }

        // Get the collision origin in the link frame
        double ox = collision->origin.position.x;
        double oy = collision->origin.position.y;
        double oz = collision->origin.position.z;
        
        // Get half dimensions
        double hx = box->dim.x / 2.0;
        double hy = box->dim.y / 2.0;
        double hz = box->dim.z / 2.0;

        // Compute the 8 corners of the box in the link frame
        std::array<geometry_msgs::msg::PointStamped, 8> corners;
        for (auto & corner : corners) {
          corner.header.frame_id = link_name;
          corner.header.stamp = rclcpp::Time(0, 0, this->get_clock()->get_clock_type());
        }

        // All 8 corners: (ox±hx, oy±hy, oz±hz)
        corners[0].point.x = ox - hx; corners[0].point.y = oy - hy; corners[0].point.z = oz - hz;
        corners[1].point.x = ox + hx; corners[1].point.y = oy - hy; corners[1].point.z = oz - hz;
        corners[2].point.x = ox - hx; corners[2].point.y = oy + hy; corners[2].point.z = oz - hz;
        corners[3].point.x = ox + hx; corners[3].point.y = oy + hy; corners[3].point.z = oz - hz;
        corners[4].point.x = ox - hx; corners[4].point.y = oy - hy; corners[4].point.z = oz + hz;
        corners[5].point.x = ox + hx; corners[5].point.y = oy - hy; corners[5].point.z = oz + hz;
        corners[6].point.x = ox - hx; corners[6].point.y = oy + hy; corners[6].point.z = oz + hz;
        corners[7].point.x = ox + hx; corners[7].point.y = oy + hy; corners[7].point.z = oz + hz;

        try {
          // Transform all corners to base_link
          for (size_t c = 0; c < 8; ++c) {
            geometry_msgs::msg::PointStamped transformed;
            transformed = tf_buffer_.transform(corners[c], "base_link", 
                                               tf2::durationFromSec(0.1));
            transformed.point.z = 0.0;
            all_points_xy.push_back(Point2{transformed.point.x, transformed.point.y});
          }

          // Only create debug markers if debug mode is enabled
          if (debug_mode) {
            // Create marker for corner points (projected to z=0 in base_link)
            visualization_msgs::msg::Marker points_marker;
            points_marker.header.frame_id = "base_link";
            points_marker.header.stamp = this->get_clock()->now();
            points_marker.ns = "collision_points";
            points_marker.id = marker_id++;
            points_marker.type = visualization_msgs::msg::Marker::POINTS;
            points_marker.action = visualization_msgs::msg::Marker::ADD;
            points_marker.scale.x = 0.02;  // Point size
            points_marker.scale.y = 0.02;
            points_marker.scale.z = 0.0;
            points_marker.color.r = 1.0;
            points_marker.color.g = 0.0;
            points_marker.color.b = 0.0;
            points_marker.color.a = 1.0;
            points_marker.lifetime = rclcpp::Duration::from_seconds(2.5);

            // Re-transform for marker points
            for (size_t c = 0; c < 8; ++c) {
              geometry_msgs::msg::PointStamped transformed;
              transformed = tf_buffer_.transform(corners[c], "base_link", 
                                                 tf2::durationFromSec(0.1));
              transformed.point.z = 0.0;
              points_marker.points.push_back(transformed.point);
            }
            points_array.markers.push_back(points_marker);

            // Create a box marker (CUBE) in base_link using the collision origin pose
            geometry_msgs::msg::PoseStamped collision_pose;
            collision_pose.header.frame_id = link_name;
            collision_pose.header.stamp = rclcpp::Time(0, 0, this->get_clock()->get_clock_type());
            collision_pose.pose.position.x = collision->origin.position.x;
            collision_pose.pose.position.y = collision->origin.position.y;
            collision_pose.pose.position.z = collision->origin.position.z;
            collision_pose.pose.orientation.x = collision->origin.rotation.x;
            collision_pose.pose.orientation.y = collision->origin.rotation.y;
            collision_pose.pose.orientation.z = collision->origin.rotation.z;
            collision_pose.pose.orientation.w = collision->origin.rotation.w;

            geometry_msgs::msg::PoseStamped collision_pose_base;
            collision_pose_base = tf_buffer_.transform(collision_pose, "base_link", tf2::durationFromSec(0.1));

            visualization_msgs::msg::Marker box_marker;
            box_marker.header.frame_id = "base_link";
            box_marker.header.stamp = this->get_clock()->now();
            box_marker.ns = "collision_boxes";
            box_marker.id = marker_id++;
            box_marker.type = visualization_msgs::msg::Marker::CUBE;
            box_marker.action = visualization_msgs::msg::Marker::ADD;
            box_marker.pose = collision_pose_base.pose;
            box_marker.scale.x = box->dim.x;
            box_marker.scale.y = box->dim.y;
            box_marker.scale.z = box->dim.z;
            box_marker.color.r = 0.0;
            box_marker.color.g = 0.5;
            box_marker.color.b = 1.0;
            box_marker.color.a = 0.15;
            box_marker.lifetime = rclcpp::Duration::from_seconds(2.5);
            boxes_array.markers.push_back(box_marker);
          }

          RCLCPP_DEBUG(this->get_logger(), 
                      "Link '%s' collision[%zu] BOX - 8 corners transformed",
                      link_name.c_str(), i);

        } catch (const tf2::TransformException & ex) {
          RCLCPP_WARN(this->get_logger(), 
                      "Could not transform '%s' to base_link: %s",
                      link_name.c_str(), ex.what());
        }
      }
    }

    // Publish the debug marker arrays (only if debug mode)
    if (debug_mode && !points_array.markers.empty()) {
      collision_points_publisher_->publish(points_array);
      RCLCPP_DEBUG(this->get_logger(), "Published %zu collision point markers", points_array.markers.size());
    }

    if (debug_mode && !boxes_array.markers.empty()) {
      collision_boxes_publisher_->publish(boxes_array);
      RCLCPP_DEBUG(this->get_logger(), "Published %zu collision box markers", boxes_array.markers.size());
    }

    // Publish convex hull over ALL projected points across the robot
    if (all_points_xy.size() >= 3) {
      const auto hull = convex_hull(std::move(all_points_xy));
      if (hull.size() >= 3) {
        const double padding = this->get_parameter("padding").as_double();

        std::vector<geometry_msgs::msg::Point> padded_hull_points;
        padded_hull_points.reserve(hull.size());
        for (const auto & p : hull) {
          geometry_msgs::msg::Point pt;
          pt.x = p.x;
          pt.y = p.y;
          pt.z = 0.0;
          padded_hull_points.push_back(pt);
        }
        padFootprint(padded_hull_points, padding);

        // Publish the hull as the nav2 footprint (Polygon type)
        {
          geometry_msgs::msg::Polygon polygon_msg;
          polygon_msg.points.reserve(padded_hull_points.size());

          for (const auto & p : padded_hull_points) {
            geometry_msgs::msg::Point32 pt;
            pt.x = static_cast<float>(p.x);
            pt.y = static_cast<float>(p.y);
            pt.z = 0.0f;
            polygon_msg.points.push_back(pt);
          }

          footprint_publisher_->publish(polygon_msg);
          RCLCPP_DEBUG(this->get_logger(), "Published /footprint convex hull with %zu vertices", hull.size());
        }

        // Only publish hull marker if debug mode
        if (debug_mode) {
          visualization_msgs::msg::Marker hull_marker;
          hull_marker.header.frame_id = "base_link";
          hull_marker.header.stamp = this->get_clock()->now();
          hull_marker.ns = "robot_convex_hull";
          hull_marker.id = 0;
          hull_marker.type = visualization_msgs::msg::Marker::LINE_STRIP;
          hull_marker.action = visualization_msgs::msg::Marker::ADD;
          hull_marker.scale.x = 0.01;  // line width
          hull_marker.color.r = 1.0;
          hull_marker.color.g = 1.0;
          hull_marker.color.b = 0.0;
          hull_marker.color.a = 1.0;
          hull_marker.lifetime = rclcpp::Duration::from_seconds(2.5);

          hull_marker.points.reserve(padded_hull_points.size() + 1);
          for (const auto & p : padded_hull_points) {
            hull_marker.points.push_back(p);
          }
          // Close the loop
          hull_marker.points.push_back(padded_hull_points.front());

          robot_hull_publisher_->publish(hull_marker);
          RCLCPP_DEBUG(this->get_logger(), "Published robot convex hull with %zu vertices (padding=%.3f)", hull.size(), padding);
        }
      }
    }
  }
};

int main(int argc, char * argv[])
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<DescriptionSubscriber>());
  rclcpp::shutdown();
  return 0;
}
