#include <memory>
#include <string>
#include <chrono>
#include <array>
#include <algorithm>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/point_cloud2.hpp"
#include "sensor_msgs/point_cloud2_iterator.hpp"
#include "geometry_msgs/msg/transform_stamped.hpp"
#include "geometry_msgs/msg/pose_stamped.hpp"
#include "geometry_msgs/msg/polygon_stamped.hpp"
#include "geometry_msgs/msg/polygon.hpp"
#include "geometry_msgs/msg/point.hpp"
#include "geometry_msgs/msg/point_stamped.hpp"
#include "tf2_msgs/msg/tf_message.hpp"
#include "std_msgs/msg/string.hpp"
#include "std_msgs/msg/float64.hpp"
#include "tf2_ros/buffer.h"
#include "tf2_ros/transform_listener.h"
#include "tf2_geometry_msgs/tf2_geometry_msgs.hpp"
#include "visualization_msgs/msg/marker.hpp"
#include "visualization_msgs/msg/marker_array.hpp"
#include "urdf/model.h"

using namespace std::chrono_literals;

/**
 * DynamicFootprint Node
 * 
 * Subscribes to:
 * - /robot_description (via ROS parameter)
 * - /tf (dynamic transforms)
 * - /tf_static (static transforms)
 */
class DynamicFootprint : public rclcpp::Node
{
public:
  explicit DynamicFootprint(const rclcpp::NodeOptions & options = rclcpp::NodeOptions())
  : rclcpp::Node("dynamic_footprint", options),
    tf_buffer_(this->get_clock()),
    tf_listener_(tf_buffer_)
  {
    RCLCPP_INFO(this->get_logger(), "DynamicFootprint node initialized");

    this->declare_parameter<double>("padding", 0.03);
    this->declare_parameter<double>("update_frequency", 15.0);
    this->declare_parameter<bool>("debug", false);

    const double update_freq = this->get_parameter("update_frequency").as_double();
    const bool debug_mode = this->get_parameter("debug").as_bool();

    // tf2_ros::TransformListener automatically subscribes to /tf and /tf_static
    // and maintains them in tf_buffer_

    // Create publisher for footprint (Polygon type for costmaps)
    footprint_publisher_ = this->create_publisher<geometry_msgs::msg::Polygon>("/footprint", 10);
    
    // Create publisher for robot height
    height_publisher_ = this->create_publisher<std_msgs::msg::Float64>("/footprint/height", 10);

    // Create publisher for arm collision points in camera optical frame
    camera_footprint_publisher_ = this->create_publisher<sensor_msgs::msg::PointCloud2>("/footprint/camera_optical", 10);

    // Only create debug publishers if debug mode is enabled
    if (debug_mode) {
      collision_points_publisher_ = this->create_publisher<visualization_msgs::msg::MarkerArray>("/footprint/collision_points", 10);
      collision_boxes_publisher_ = this->create_publisher<visualization_msgs::msg::MarkerArray>("/footprint/collision_boxes", 10);
      robot_hull_publisher_ = this->create_publisher<visualization_msgs::msg::Marker>("/footprint/robot_convex_hull", 10);
      height_point_publisher_ = this->create_publisher<geometry_msgs::msg::PointStamped>("/footprint/height_point", 10);
    }

    // Create a timer based on update_frequency param
    const auto period = std::chrono::duration<double>(1.0 / update_freq);
    timer_ = this->create_wall_timer(
      std::chrono::duration_cast<std::chrono::nanoseconds>(period),
      std::bind(&DynamicFootprint::timer_callback, this));

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
  rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr height_publisher_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr camera_footprint_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr collision_points_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::MarkerArray>::SharedPtr collision_boxes_publisher_;
  rclcpp::Publisher<visualization_msgs::msg::Marker>::SharedPtr robot_hull_publisher_;
  rclcpp::Publisher<geometry_msgs::msg::PointStamped>::SharedPtr height_point_publisher_;
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

  // ===========================================================================
  // Helper utilities for footprint projection
  // ===========================================================================

  /**
   * Check if a link name belongs to the arm kinematic chain.
   */
  static bool isArmLink(const std::string & name)
  {
    // link1-link5 are arm joints, link61/link62 are gripper fingers
    static const std::array<std::string, 7> arm_links = {
      "link1", "link2", "link3", "link4", "link5", "link61", "link62"
    };
    for (const auto & a : arm_links) {
      if (name == a) return true;
    }
    return false;
  }

  /**
   * Apply a pre-looked-up TF transform to a batch of 3D points.
   */
  static std::vector<geometry_msgs::msg::Point> applyTransform(
      const std::vector<geometry_msgs::msg::Point>& points,
      const geometry_msgs::msg::TransformStamped& tf)
  {
    std::vector<geometry_msgs::msg::Point> out;
    out.reserve(points.size());
    for (const auto & p : points) {
      geometry_msgs::msg::PointStamped ps_in, ps_out;
      ps_in.point = p;
      tf2::doTransform(ps_in, ps_out, tf);
      out.push_back(ps_out.point);
    }
    return out;
  }

  /**
   * Project 3D points onto the XY plane and return the 2D convex hull.
   */
  static std::vector<Point2> projectXYConvexHull(
      const std::vector<geometry_msgs::msg::Point>& pts_3d)
  {
    std::vector<Point2> pts_2d;
    pts_2d.reserve(pts_3d.size());
    for (const auto & p : pts_3d) {
      pts_2d.push_back(Point2{p.x, p.y});
    }
    return convex_hull(std::move(pts_2d));
  }

  /**
   * Build a Polygon message from 2D hull points.
   */
  static geometry_msgs::msg::Polygon toPolygonMsg(const std::vector<Point2>& hull)
  {
    geometry_msgs::msg::Polygon msg;
    msg.points.reserve(hull.size());
    for (const auto & p : hull) {
      geometry_msgs::msg::Point32 pt;
      pt.x = static_cast<float>(p.x);
      pt.y = static_cast<float>(p.y);
      pt.z = 0.0f;
      msg.points.push_back(pt);
    }
    return msg;
  }

  /**
   * Publish arm collision corners as a PointCloud2 in camera_optical_frame.
   */
  void publishCameraFootprint(
      const std::vector<geometry_msgs::msg::Point>& corners_base_link)
  {
    if (corners_base_link.empty()) return;
    try {
      const auto tf = tf_buffer_.lookupTransform(
          "camera_optical_frame", "base_link",
          tf2::TimePointZero, tf2::durationFromSec(0.1));

      const auto cam_pts = applyTransform(corners_base_link, tf);

      auto cloud = std::make_unique<sensor_msgs::msg::PointCloud2>();
      cloud->header.stamp    = this->get_clock()->now();
      cloud->header.frame_id = "camera_optical_frame";
      cloud->height = 1;
      cloud->width  = static_cast<uint32_t>(cam_pts.size());
      cloud->is_dense = true;
      cloud->is_bigendian = false;

      sensor_msgs::PointCloud2Modifier mod(*cloud);
      mod.setPointCloud2FieldsByString(1, "xyz");
      mod.resize(cam_pts.size());

      sensor_msgs::PointCloud2Iterator<float> ix(*cloud, "x");
      sensor_msgs::PointCloud2Iterator<float> iy(*cloud, "y");
      sensor_msgs::PointCloud2Iterator<float> iz(*cloud, "z");

      for (const auto & p : cam_pts) {
        *ix = static_cast<float>(p.x);
        *iy = static_cast<float>(p.y);
        *iz = static_cast<float>(p.z);
        ++ix; ++iy; ++iz;
      }

      camera_footprint_publisher_->publish(std::move(cloud));
      RCLCPP_DEBUG(this->get_logger(),
                   "Published /footprint/camera_optical with %zu points",
                   cam_pts.size());
    } catch (const tf2::TransformException & ex) {
      RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                           "Camera footprint TF lookup failed: %s", ex.what());
    }
  }

  /**
   * Process collision boxes from each link and transform to base_link frame
   */
  void process_collision_boxes()
  {
    const bool debug_mode = this->get_parameter("debug").as_bool();
    const double padding = this->get_parameter("padding").as_double();
    visualization_msgs::msg::MarkerArray points_array;
    visualization_msgs::msg::MarkerArray boxes_array;
    int marker_id = 0;

    std::vector<Point2> all_points_xy;
    all_points_xy.reserve(512);
    std::vector<geometry_msgs::msg::Point> all_corners_3d;  // 3D corners (all links)
    all_corners_3d.reserve(512);
    std::vector<geometry_msgs::msg::Point> arm_corners_3d;   // 3D corners (arm only) for camera projection
    arm_corners_3d.reserve(256);
    double max_z = 0.0;  // Track maximum height of collision geometry
    geometry_msgs::msg::PointStamped tallest_point;  // Track the tallest point
    tallest_point.header.frame_id = "base_link";

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

        // Get half dimensions for local box corners
        double hx = box->dim.x / 2.0;
        double hy = box->dim.y / 2.0;
        double hz = box->dim.z / 2.0;

        // Build collision origin transform (position + rotation)
        tf2::Transform collision_tf;
        collision_tf.setOrigin(tf2::Vector3(
          collision->origin.position.x,
          collision->origin.position.y,
          collision->origin.position.z));
        collision_tf.setRotation(tf2::Quaternion(
          collision->origin.rotation.x,
          collision->origin.rotation.y,
          collision->origin.rotation.z,
          collision->origin.rotation.w));

        // Compute the 8 corners of the box in the link frame (with rotation applied)
        std::array<geometry_msgs::msg::PointStamped, 8> corners;
        const double local_corners[8][3] = {
          {-hx, -hy, -hz}, {+hx, -hy, -hz}, {-hx, +hy, -hz}, {+hx, +hy, -hz},
          {-hx, -hy, +hz}, {+hx, -hy, +hz}, {-hx, +hy, +hz}, {+hx, +hy, +hz}
        };
        for (size_t c = 0; c < 8; ++c) {
          tf2::Vector3 local_pt(local_corners[c][0], local_corners[c][1], local_corners[c][2]);
          tf2::Vector3 link_pt = collision_tf * local_pt;  // Apply collision origin transform
          corners[c].header.frame_id = link_name;
          corners[c].header.stamp = rclcpp::Time(0, 0, this->get_clock()->get_clock_type());
          corners[c].point.x = link_pt.x();
          corners[c].point.y = link_pt.y();
          corners[c].point.z = link_pt.z();
        }

        try {
          // Transform all corners to base_link
          for (size_t c = 0; c < 8; ++c) {
            geometry_msgs::msg::PointStamped transformed;
            transformed = tf_buffer_.transform(corners[c], "base_link", 
                                               tf2::durationFromSec(0.1));
            // Track max Z before projecting to XY plane
            if (transformed.point.z > max_z) {
              max_z = transformed.point.z;
              tallest_point = transformed;
              tallest_point.header.stamp = this->get_clock()->now();
            }
            all_corners_3d.push_back(transformed.point);
            if (isArmLink(link_name)) {
              arm_corners_3d.push_back(transformed.point);
            }
            transformed.point.z = 0.0;
            all_points_xy.push_back(Point2{transformed.point.x, transformed.point.y});
          }

          // Only create debug markers if debug mode is enabled
          if (debug_mode) {
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
            visualization_msgs::msg::Marker points_marker_3d = points_marker;


            // Re-transform for marker points
            for (size_t c = 0; c < 8; ++c) {
              geometry_msgs::msg::PointStamped transformed;
              transformed = tf_buffer_.transform(corners[c], "base_link", 
                                                 tf2::durationFromSec(0.1));
              transformed.point.z = 0.0;
              points_marker.points.push_back(transformed.point);
            }
            boxes_array.markers.push_back(points_marker);


            points_marker_3d.scale.z = 0.02;  // Enable 3D visualization
            // Add 3D points (with actual Z values)
            for (size_t c = 0; c < 8; ++c) {
              geometry_msgs::msg::PointStamped transformed;
              transformed = tf_buffer_.transform(corners[c], "base_link", 
                                                 tf2::durationFromSec(0.1));
              points_marker_3d.points.push_back(transformed.point);
            }
            points_array.markers.push_back(points_marker_3d);

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

        // Publish arm-only footprint projected into camera optical frame
        publishCameraFootprint(arm_corners_3d);

        // Publish the max height of the robot collision geometry
        {
          std_msgs::msg::Float64 height_msg;
          height_msg.data = max_z + padding;
          height_publisher_->publish(height_msg);
          RCLCPP_DEBUG(this->get_logger(), "Published /footprint/height: %.3f m (max_z=%.3f, padding=%.3f)", 
                       height_msg.data, max_z, padding);
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

          // Publish the tallest point
          if (debug_mode && height_point_publisher_ && max_z > 0) {
            tallest_point.point.z += padding;
            height_point_publisher_->publish(tallest_point);
            RCLCPP_DEBUG(this->get_logger(), "Published /footprint/height_point: (%.3f, %.3f, %.3f)", 
                         tallest_point.point.x, tallest_point.point.y, tallest_point.point.z);
          }
        }
      }
    }
  }
};

int main(int argc, char * argv[])
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<DynamicFootprint>());
  rclcpp::shutdown();
  return 0;
}
