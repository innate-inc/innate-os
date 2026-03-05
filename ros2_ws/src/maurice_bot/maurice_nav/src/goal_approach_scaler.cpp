/**
 * Scales down cmd_vel as the robot approaches the navigation goal.
 *
 * Sits between the controller_server and velocity_smoother:
 *   controller_server -> /cmd_vel_raw -> [this node] -> /cmd_vel_scaled -> velocity_smoother
 *
 * Uses the NavigateToPose action feedback (distance_remaining) to determine
 * how close the robot is to the goal — no TF lookup needed.
 */

#include <cmath>
#include <limits>

#include "rclcpp/rclcpp.hpp"
#include "geometry_msgs/msg/twist.hpp"
#include "nav2_msgs/action/navigate_to_pose.hpp"
#include "action_msgs/msg/goal_status_array.hpp"

class GoalApproachScaler : public rclcpp::Node
{
public:
  GoalApproachScaler()
  : Node("goal_approach_scaler"),
    distance_remaining_(std::numeric_limits<double>::infinity()),
    navigating_(false)
  {
    // Parameters
    this->declare_parameter("slowdown_radius", 0.3);
    this->declare_parameter("min_speed_fraction", 0.3);

    slowdown_radius_ = this->get_parameter("slowdown_radius").as_double();
    min_speed_fraction_ = this->get_parameter("min_speed_fraction").as_double();

    // Subscribe to NavigateToPose action feedback (published by bt_navigator)
    using FeedbackMsg = nav2_msgs::action::NavigateToPose::Impl::FeedbackMessage;
    feedback_sub_ = this->create_subscription<FeedbackMsg>(
      "/internal_navigate_to_pose/_action/feedback", 10,
      [this](const FeedbackMsg::SharedPtr msg) {
        distance_remaining_ = msg->feedback.distance_remaining;
        navigating_ = true;
      });

    // Subscribe to action status to detect when navigation ends
    status_sub_ = this->create_subscription<action_msgs::msg::GoalStatusArray>(
      "/internal_navigate_to_pose/_action/status", 10,
      [this](const action_msgs::msg::GoalStatusArray::SharedPtr msg) {
        if (msg->status_list.empty()) {
          navigating_ = false;
          distance_remaining_ = std::numeric_limits<double>::infinity();
          return;
        }
        // status 2 = EXECUTING, anything else means done/aborted/canceled
        bool active = false;
        for (const auto & s : msg->status_list) {
          if (s.status == 2) {
            active = true;
            break;
          }
        }
        if (!active) {
          navigating_ = false;
          distance_remaining_ = std::numeric_limits<double>::infinity();
        }
      });

    // cmd_vel passthrough
    vel_sub_ = this->create_subscription<geometry_msgs::msg::Twist>(
      "cmd_vel_in", 10,
      std::bind(&GoalApproachScaler::vel_cb, this, std::placeholders::_1));
    vel_pub_ = this->create_publisher<geometry_msgs::msg::Twist>("cmd_vel_out", 10);

    RCLCPP_INFO(this->get_logger(),
      "Goal approach scaler: radius=%.2fm, min_fraction=%.2f",
      slowdown_radius_, min_speed_fraction_);
  }

private:
  void vel_cb(const geometry_msgs::msg::Twist::SharedPtr msg)
  {
    if (!navigating_ || distance_remaining_ >= slowdown_radius_) {
      vel_pub_->publish(*msg);
      return;
    }

    // Linear ramp: at dist==0 -> min_speed_fraction, at dist==radius -> 1.0
    double fraction = min_speed_fraction_ + (1.0 - min_speed_fraction_) *
      (distance_remaining_ / slowdown_radius_);

    geometry_msgs::msg::Twist scaled;
    scaled.linear.x = msg->linear.x * fraction;
    scaled.linear.y = msg->linear.y;
    scaled.linear.z = msg->linear.z;
    scaled.angular.x = msg->angular.x;
    scaled.angular.y = msg->angular.y;
    scaled.angular.z = msg->angular.z;
    vel_pub_->publish(scaled);
  }

  double slowdown_radius_;
  double min_speed_fraction_;
  double distance_remaining_;
  bool navigating_;

  rclcpp::Subscription<nav2_msgs::action::NavigateToPose::Impl::FeedbackMessage>::SharedPtr feedback_sub_;
  rclcpp::Subscription<action_msgs::msg::GoalStatusArray>::SharedPtr status_sub_;
  rclcpp::Subscription<geometry_msgs::msg::Twist>::SharedPtr vel_sub_;
  rclcpp::Publisher<geometry_msgs::msg::Twist>::SharedPtr vel_pub_;
};

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<GoalApproachScaler>());
  rclcpp::shutdown();
  return 0;
}
