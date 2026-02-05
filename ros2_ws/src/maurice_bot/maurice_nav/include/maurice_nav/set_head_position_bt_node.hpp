// Copyright (c) 2024 Maurice Robotics
// Licensed under the Apache License, Version 2.0

#pragma once

#include <string>
#include <memory>
#include <chrono>

#include "rclcpp/rclcpp.hpp"
#include "std_msgs/msg/int32.hpp"
#include "behaviortree_cpp_v3/action_node.h"

namespace maurice_nav
{

/**
 * @brief A BT action node that publishes head position to /mars/head/set_position.
 * 
 * Usage in behavior tree XML:
 * <SetHeadPosition position="-20" topic="/mars/head/set_position"/>
 * 
 * Parameters:
 *   - position: The head angle in degrees (integer). Default: -20
 *               Negative = looking down, Positive = looking up
 *               Typical range: -25 to +15
 *   - topic: The topic to publish to. Default: /mars/head/set_position
 */
class SetHeadPosition : public BT::SyncActionNode
{
public:
  SetHeadPosition(const std::string& name, const BT::NodeConfiguration& config)
    : BT::SyncActionNode(name, config)
  {
    // Create a minimal ROS2 node for publishing
    if (!rclcpp::ok()) {
      rclcpp::init(0, nullptr);
    }
    
    node_ = std::make_shared<rclcpp::Node>("set_head_position_bt_node");
  }

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<int>("position", -20, "Head position in degrees (-25 to +15)"),
      BT::InputPort<std::string>("topic", "/mars/head/set_position", "Topic to publish head position to"),
    };
  }

  BT::NodeStatus tick() override
  {
    int position = -20;
    std::string topic = "/mars/head/set_position";
    
    getInput("position", position);
    getInput("topic", topic);

    // Create publisher if not exists or topic changed
    if (!publisher_ || current_topic_ != topic) {
      publisher_ = node_->create_publisher<std_msgs::msg::Int32>(topic, 10);
      current_topic_ = topic;
      // Small delay to allow publisher to be discovered
      rclcpp::sleep_for(std::chrono::milliseconds(50));
    }

    // Publish the head position
    auto msg = std_msgs::msg::Int32();
    msg.data = position;
    publisher_->publish(msg);

    RCLCPP_INFO(node_->get_logger(), "SetHeadPosition: Published head position %d to %s", 
                position, topic.c_str());

    return BT::NodeStatus::SUCCESS;
  }

private:
  std::shared_ptr<rclcpp::Node> node_;
  rclcpp::Publisher<std_msgs::msg::Int32>::SharedPtr publisher_;
  std::string current_topic_;
};

}  // namespace maurice_nav
