// Copyright (c) 2024 Maurice Robotics
// Licensed under the Apache License, Version 2.0

// BT node plugin registration - includes all custom BT nodes

#include "maurice_nav/switch_bt_node.hpp"
#include "maurice_nav/set_head_position_bt_node.hpp"
#include "behaviortree_cpp_v3/bt_factory.h"

// This is the correct registration function for BT.CPP v3 plugins
extern "C" void BT_RegisterNodesFromPlugin(BT::BehaviorTreeFactory& factory)
{
  factory.registerNodeType<maurice_nav::Switch>("Switch");
  factory.registerNodeType<maurice_nav::SetHeadPosition>("SetHeadPosition");
}