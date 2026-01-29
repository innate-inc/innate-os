#include <chrono>
#include <memory>
#include <string>

#include "geometry_msgs/msg/transform_stamped.hpp"
#include "lifecycle_msgs/msg/state.hpp"
#include "nav_msgs/msg/occupancy_grid.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_lifecycle/lifecycle_node.hpp"
#include "rclcpp_lifecycle/lifecycle_publisher.hpp"
#include "tf2/LinearMath/Quaternion.h"
#include "tf2_ros/transform_broadcaster.h"

using namespace std::chrono_literals;

/// NullMapNode inheriting from rclcpp_lifecycle::LifecycleNode
/**
 * The NullMapNode is a lifecycle node that publishes an identity transform
 * from map to odom frame. This is useful when a robot doesn't have SLAM
 * and wants to assume the map and odom frames are aligned.
 */
class NullMapNode : public rclcpp_lifecycle::LifecycleNode
{
public:
  /// NullMapNode constructor
  /**
   * The NullMapNode constructor initializes the lifecycle node with
   * the node name "null_map_node".
   */
  explicit NullMapNode(const std::string & node_name = "null_map_node",
      bool intra_process_comms = false)
  : rclcpp_lifecycle::LifecycleNode(node_name,
      rclcpp::NodeOptions().use_intra_process_comms(intra_process_comms))
  {}

  /// Callback for walltimer to publish the identity transform
  /**
   * Callback for the wall timer. This function gets invoked periodically
   * by the timer and publishes an identity transform from map to odom.
   */
  void
  publish_transform()
  {
    // Only publish if the node is active
    if (this->get_current_state().id() != lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE) {
      return;
    }

    geometry_msgs::msg::TransformStamped t;

    // Set header information
    t.header.stamp = this->get_clock()->now();
    t.header.frame_id = "map";
    t.child_frame_id = "odom";

    // Identity transform - no translation
    t.transform.translation.x = 0.0;
    t.transform.translation.y = 0.0;
    t.transform.translation.z = 0.0;

    // Identity rotation (no rotation)
    tf2::Quaternion q;
    q.setRPY(0, 0, 0);
    t.transform.rotation.x = q.x();
    t.transform.rotation.y = q.y();
    t.transform.rotation.z = q.z();
    t.transform.rotation.w = q.w();

    // Send the transformation
    tf_broadcaster_->sendTransform(t);

    // Publish empty map
    publish_empty_map();
  }

  /// Callback to publish an empty occupancy grid
  /**
   * Publishes an empty map (0x0 size) to the /map topic.
   */
  void
  publish_empty_map()
  {
    if (!map_pub_ || !map_pub_->is_activated()) {
      return;
    }

    auto map_msg = std::make_unique<nav_msgs::msg::OccupancyGrid>();

    // Set header
    map_msg->header.stamp = this->get_clock()->now();
    map_msg->header.frame_id = "map";

    // Set minimal map (1x1 size)
    map_msg->info.resolution = 0.05;  // 5 cm per cell
    map_msg->info.width = 1;
    map_msg->info.height = 1;
    map_msg->info.origin.position.x = 0.0;
    map_msg->info.origin.position.y = 0.0;
    map_msg->info.origin.position.z = 0.0;
    map_msg->info.origin.orientation.x = 0.0;
    map_msg->info.origin.orientation.y = 0.0;
    map_msg->info.origin.orientation.z = 0.0;
    map_msg->info.origin.orientation.w = 1.0;

    // Single cell data for 1x1 map (0 = free space)
    map_msg->data = {0};

    // Publish the empty map
    map_pub_->publish(std::move(map_msg));
  }

  /// Transition callback for state configuring
  /**
   * on_configure callback is called when the lifecycle node
   * enters the "configuring" state.
   * Initialize and configure the transform broadcaster and timer.
   */
  rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn
  on_configure(const rclcpp_lifecycle::State &)
  {
    // Initialize the transform broadcaster
    tf_broadcaster_ =
      std::make_unique<tf2_ros::TransformBroadcaster>(*this);

    // Create a lifecycle publisher for the map
    map_pub_ = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
      "/map", rclcpp::QoS(rclcpp::KeepLast(1)).transient_local().reliable());

    // Create a wall timer to publish at 10 Hz
    timer_ = this->create_wall_timer(
      100ms, [this]() {return this->publish_transform();});

    RCLCPP_INFO(get_logger(), "on_configure() is called.");

    return rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn::SUCCESS;
  }

  /// Transition callback for state activating
  /**
   * on_activate callback is called when the lifecycle node
   * enters the "activating" state.
   */
  rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn
  on_activate(const rclcpp_lifecycle::State & state)
  {
    // Activate the lifecycle publisher
    map_pub_->on_activate();
    
    LifecycleNode::on_activate(state);

    RCLCPP_INFO(get_logger(), "on_activate() is called.");

    return rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn::SUCCESS;
  }

  /// Transition callback for state deactivating
  /**
   * on_deactivate callback is called when the lifecycle node
   * enters the "deactivating" state.
   */
  rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn
  on_deactivate(const rclcpp_lifecycle::State & state)
  {
    // Deactivate the lifecycle publisher
    map_pub_->on_deactivate();
    
    LifecycleNode::on_deactivate(state);

    RCLCPP_INFO(get_logger(), "on_deactivate() is called.");

    return rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn::SUCCESS;
  }

  /// Transition callback for state cleaningup
  /**
   * on_cleanup callback is called when the lifecycle node
   * enters the "cleaningup" state.
   * Release resources (timer).
   */
  rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn
  on_cleanup(const rclcpp_lifecycle::State &)
  {
    // Release the timer and publisher
    timer_.reset();
    map_pub_.reset();

    RCLCPP_INFO(get_logger(), "on_cleanup() is called.");

    return rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn::SUCCESS;
  }

  /// Transition callback for state shutting down
  /**
   * on_shutdown callback is called when the lifecycle node
   * enters the "shuttingdown" state.
   */
  rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn
  on_shutdown(const rclcpp_lifecycle::State & state)
  {
    // Release the timer and publisher
    timer_.reset();
    map_pub_.reset();

    RCLCPP_INFO(get_logger(), "on_shutdown() is called from state %s.", state.label().c_str());

    return rclcpp_lifecycle::node_interfaces::LifecycleNodeInterface::CallbackReturn::SUCCESS;
  }

private:
  // Transform broadcaster for publishing tf transforms
  std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;

  // Lifecycle publisher for the empty map
  std::shared_ptr<rclcpp_lifecycle::LifecyclePublisher<nav_msgs::msg::OccupancyGrid>> map_pub_;

  // Timer for periodic publishing
  std::shared_ptr<rclcpp::TimerBase> timer_;
};

int main(int argc, char * argv[])
{
  // Force flush of stdout buffer for correct sync of prints
  setvbuf(stdout, NULL, _IONBF, BUFSIZ);

  rclcpp::init(argc, argv);

  rclcpp::executors::SingleThreadedExecutor exe;

  std::shared_ptr<NullMapNode> node = std::make_shared<NullMapNode>();

  exe.add_node(node->get_node_base_interface());

  exe.spin();

  rclcpp::shutdown();

  return 0;
}
