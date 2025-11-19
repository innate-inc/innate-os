#include "maurice_arm/planner.hpp"
#include <chrono>

namespace maurice_arm {

MauricePlanner::MauricePlanner(const rclcpp::Node::SharedPtr& node)
    : node_(node) {
    
    RCLCPP_INFO(node_->get_logger(), "Initializing MoveIt planner...");
    
    // Create MoveGroup interface for "arm" planning group
    // This reads all the config from moveit.yaml, URDF, SRDF
    move_group_ = std::make_shared<moveit::planning_interface::MoveGroupInterface>(
        node_, "arm");
    
    // Create planning scene interface (for obstacles, ground plane, etc.)
    planning_scene_ = std::make_shared<moveit::planning_interface::PlanningSceneInterface>();
    
    // Configure move group
    move_group_->setPlanningTime(5.0);               // Default 5 seconds
    move_group_->setNumPlanningAttempts(3);          // Try 3 times
    move_group_->setMaxVelocityScalingFactor(1.0);   // 100% of max velocity
    move_group_->setMaxAccelerationScalingFactor(1.0);
    move_group_->setPlannerId("RRTstar");            // Use RRTstar (from config)
    
    RCLCPP_INFO(node_->get_logger(), "MoveIt planner initialized!");
    RCLCPP_INFO(node_->get_logger(), "  Planning group: %s", 
                move_group_->getName().c_str());
    RCLCPP_INFO(node_->get_logger(), "  Planning frame: %s", 
                move_group_->getPlanningFrame().c_str());
    RCLCPP_INFO(node_->get_logger(), "  End effector: %s", 
                move_group_->getEndEffectorLink().c_str());
    RCLCPP_INFO(node_->get_logger(), "  DOF: %zu", 
                move_group_->getVariableCount());
}

PlanResult MauricePlanner::planToGoal(
    const std::vector<double>& goal_positions,
    double planning_time) {
    
    PlanResult result;
    result.success = false;
    
    auto start_time = std::chrono::high_resolution_clock::now();
    
    // Validate input
    if (goal_positions.size() != 5) {
        result.error_message = "Expected 5 joint positions, got " + 
                              std::to_string(goal_positions.size());
        RCLCPP_ERROR(node_->get_logger(), "%s", result.error_message.c_str());
        return result;
    }
    
    // Set planning time
    move_group_->setPlanningTime(planning_time);
    
    // Set joint target
    move_group_->setJointValueTarget(goal_positions);
    
    RCLCPP_INFO(node_->get_logger(), 
                "Planning to goal: [%.3f, %.3f, %.3f, %.3f, %.3f]",
                goal_positions[0], goal_positions[1], goal_positions[2], 
                goal_positions[3], goal_positions[4]);
    
    // Plan
    moveit::planning_interface::MoveGroupInterface::Plan plan;
    auto error_code = move_group_->plan(plan);
    
    auto end_time = std::chrono::high_resolution_clock::now();
    double planning_time_ms = 
        std::chrono::duration<double, std::milli>(end_time - start_time).count();
    
    if (error_code == moveit::core::MoveItErrorCode::SUCCESS) {
        result = convertPlan(plan, planning_time_ms);
        
        RCLCPP_INFO(node_->get_logger(), 
                   "✓ Planning succeeded in %.1fms: %d waypoints",
                   result.planning_time_ms, result.num_waypoints);
    } else {
        result.error_message = "Planning failed with error code: " + 
                              std::to_string(error_code.val);
        result.planning_time_ms = planning_time_ms;
        RCLCPP_ERROR(node_->get_logger(), "%s", result.error_message.c_str());
    }
    
    return result;
}

PlanResult MauricePlanner::planToNamedTarget(
    const std::string& state_name,
    double planning_time) {
    
    PlanResult result;
    result.success = false;
    
    auto start_time = std::chrono::high_resolution_clock::now();
    
    // Set named target (like "home" from SRDF)
    move_group_->setNamedTarget(state_name);
    move_group_->setPlanningTime(planning_time);
    
    RCLCPP_INFO(node_->get_logger(), "Planning to named state: %s", state_name.c_str());
    
    // Plan
    moveit::planning_interface::MoveGroupInterface::Plan plan;
    auto error_code = move_group_->plan(plan);
    
    auto end_time = std::chrono::high_resolution_clock::now();
    double planning_time_ms = 
        std::chrono::duration<double, std::milli>(end_time - start_time).count();
    
    if (error_code == moveit::core::MoveItErrorCode::SUCCESS) {
        result = convertPlan(plan, planning_time_ms);
        RCLCPP_INFO(node_->get_logger(), 
                   "✓ Planning to '%s' succeeded in %.1fms",
                   state_name.c_str(), result.planning_time_ms);
    } else {
        result.error_message = "Planning to '" + state_name + "' failed";
        result.planning_time_ms = planning_time_ms;
        RCLCPP_ERROR(node_->get_logger(), "%s", result.error_message.c_str());
    }
    
    return result;
}

bool MauricePlanner::isInCollision(const std::vector<double>& joint_positions) {
    if (joint_positions.size() != 5) {
        RCLCPP_ERROR(node_->get_logger(), "Expected 5 joints for collision check");
        return true;  // Conservative: assume collision
    }
    
    // Set the state to check
    move_group_->setJointValueTarget(joint_positions);
    
    // Check if valid (within bounds and collision-free)
    // Note: This is a simplified check. For full collision checking,
    // you'd need to query the planning scene directly.
    return !move_group_->setJointValueTarget(joint_positions);
}

std::vector<double> MauricePlanner::getCurrentJointPositions() {
    return move_group_->getCurrentJointValues();
}

void MauricePlanner::updateCurrentState(const std::vector<double>& current_positions) {
    if (current_positions.size() != 5) {
        RCLCPP_WARN(node_->get_logger(), 
                    "Cannot update state: expected 5 joints, got %zu", 
                    current_positions.size());
        return;
    }
    
    // Set current state as starting state for planning
    move_group_->setStartState(*move_group_->getCurrentState());
    
    RCLCPP_DEBUG(node_->get_logger(), 
                "Updated current state: [%.3f, %.3f, %.3f, %.3f, %.3f]",
                current_positions[0], current_positions[1], current_positions[2],
                current_positions[3], current_positions[4]);
}

PlanResult MauricePlanner::convertPlan(
    const moveit::planning_interface::MoveGroupInterface::Plan& plan,
    double planning_time_ms) {
    
    PlanResult result;
    result.success = true;
    result.planning_time_ms = planning_time_ms;
    
    const auto& trajectory = plan.trajectory_.joint_trajectory;
    result.num_waypoints = trajectory.points.size();
    
    if (result.num_waypoints == 0) {
        result.success = false;
        result.error_message = "Empty trajectory";
        return result;
    }
    
    // Extract waypoints
    for (const auto& point : trajectory.points) {
        // Joint positions (5 joints)
        result.waypoints.push_back(point.positions);
        
        // Time from start
        double time_sec = point.time_from_start.sec + 
                         point.time_from_start.nanosec * 1e-9;
        result.time_from_start.push_back(time_sec);
    }
    
    return result;
}

} // namespace maurice_arm

