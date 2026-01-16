// Copyright (c) 2024 Maurice Robotics
// Licensed under the Apache License, Version 2.0

#include <string>
#include "behaviortree_cpp_v3/control_node.h"

namespace maurice_nav
{

/**
 * @brief A Switch control node that selects which child to execute based on a 
 * string variable matching child names. Children should have name="value" attributes.
 * 
 * Usage:
 * <Switch variable="{selected_planner}">
 *     <SomeNode name="mapfree" .../>
 *     <SomeNode name="navigation" .../>
 * </Switch>
 * 
 * The child whose name matches the variable value will be ticked.
 * If no match is found, the first child is ticked as default.
 */
class Switch : public BT::ControlNode
{
public:
  Switch(const std::string& name, const BT::NodeConfiguration& config)
    : BT::ControlNode(name, config)
  {
  }

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("variable", "The blackboard variable to switch on"),
    };
  }

  BT::NodeStatus tick() override
  {
    std::string variable_value;
    if (!getInput("variable", variable_value)) {
      // If we can't get the variable, use first child as default
      variable_value = "";
    }

    // Find child with matching name
    int selected_child = 0;  // Default to first child
    for (size_t i = 0; i < children_nodes_.size(); i++) {
      if (children_nodes_[i]->name() == variable_value) {
        selected_child = static_cast<int>(i);
        break;
      }
    }

    // Halt all non-selected children that might be running
    for (size_t i = 0; i < children_nodes_.size(); i++) {
      if (static_cast<int>(i) != selected_child) {
        haltChild(i);
      }
    }

    // Tick the selected child
    BT::NodeStatus child_status = children_nodes_[selected_child]->executeTick();
    
    if (child_status == BT::NodeStatus::RUNNING) {
      return BT::NodeStatus::RUNNING;
    }

    // Reset all children for next tick
    haltChildren();
    return child_status;
  }

  void halt() override
  {
    haltChildren();
    ControlNode::halt();
  }
};

}  // namespace maurice_nav

#include "behaviortree_cpp_v3/bt_factory.h"

// This is the correct registration function for BT.CPP v3 plugins
extern "C" void BT_RegisterNodesFromPlugin(BT::BehaviorTreeFactory& factory)
{
  factory.registerNodeType<maurice_nav::Switch>("Switch");
}
