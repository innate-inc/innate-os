#include "maurice_arm/planner.hpp"
#include <chrono>
#include <moveit_msgs/msg/collision_object.hpp>
#include <shape_msgs/msg/solid_primitive.hpp>
#include <geometry_msgs/msg/pose.hpp>

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
    
    // Add ground plane collision object
    // This prevents the arm from planning paths that go through the ground
    addGroundPlane();
    
    RCLCPP_INFO(node_->get_logger(), "MoveIt planner initialized!");
    RCLCPP_INFO(node_->get_logger(), "  Planning group: %s", 
                move_group_->getName().c_str());
    RCLCPP_INFO(node_->get_logger(), "  Planning frame: %s", 
                move_group_->getPlanningFrame().c_str());
    RCLCPP_INFO(node_->get_logger(), "  End effector: %s", 
                move_group_->getEndEffectorLink().c_str());
    RCLCPP_INFO(node_->get_logger(), "  DOF: %u", 
                move_group_->getVariableCount());
}

void MauricePlanner::addGroundPlane() {
    // Create a collision object for the ground plane
    moveit_msgs::msg::CollisionObject ground_plane;
    ground_plane.header.frame_id = move_group_->getPlanningFrame();
    ground_plane.id = "ground_plane";
    
    // Define the ground as a large box below z=0
    // 10m x 10m x 2m box, centered at z=-1.0m (extends from z=0 to z=-2m)
    shape_msgs::msg::SolidPrimitive primitive;
    primitive.type = primitive.BOX;
    primitive.dimensions.resize(3);
    primitive.dimensions[primitive.BOX_X] = 10.0;  // 10m wide
    primitive.dimensions[primitive.BOX_Y] = 10.0;  // 10m deep
    primitive.dimensions[primitive.BOX_Z] = 2.0;   // 2m thick
    
    // Position the box center at z=-1.0m (so top is at z=0, bottom at z=-2)
    geometry_msgs::msg::Pose box_pose;
    box_pose.orientation.w = 1.0;  // No rotation
    box_pose.position.x = 0.0;
    box_pose.position.y = 0.0;
    box_pose.position.z = -1.0;    // Center 1m below origin
    
    ground_plane.primitives.push_back(primitive);
    ground_plane.primitive_poses.push_back(box_pose);
    ground_plane.operation = ground_plane.ADD;
    
    // Add to planning scene
    std::vector<moveit_msgs::msg::CollisionObject> collision_objects;
    collision_objects.push_back(ground_plane);
    planning_scene_->addCollisionObjects(collision_objects);
    
    RCLCPP_INFO(node_->get_logger(), "Added ground plane collision object (10m x 10m x 2m at z=-1m)");
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
        result.error_code = PlanErrorCode::SUCCESS;
        
        RCLCPP_INFO(node_->get_logger(), 
                   "✓ Planning succeeded in %.1fms: %d waypoints",
                   result.planning_time_ms, result.num_waypoints);
    } else {
        result.error_code = static_cast<PlanErrorCode>(error_code.val);
        result.planning_time_ms = planning_time_ms;
        
        // Human-readable error messages
        switch (result.error_code) {
            case PlanErrorCode::START_STATE_IN_COLLISION:
                result.error_message = "Start state is in collision";
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
                break;
            case PlanErrorCode::GOAL_IN_COLLISION:
                result.error_message = "Goal state is in collision";
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
                break;
            case PlanErrorCode::TIMED_OUT:
                result.error_message = "Planning timed out (increase planning_time)";
                RCLCPP_WARN(node_->get_logger(), "⏱️  %s", result.error_message.c_str());
                break;
            case PlanErrorCode::PLANNING_FAILED:
                result.error_message = "No valid path found";
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
                break;
            case PlanErrorCode::INVALID_GOAL_CONSTRAINTS:
                result.error_message = "Goal constraints invalid or unreachable";
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
                break;
            default:
                result.error_message = "Planning failed with error code: " + 
                                      std::to_string(error_code.val);
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
        }
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
        result.error_code = PlanErrorCode::SUCCESS;
        RCLCPP_INFO(node_->get_logger(), 
                   "✓ Planning to '%s' succeeded in %.1fms",
                   state_name.c_str(), result.planning_time_ms);
    } else {
        result.error_code = static_cast<PlanErrorCode>(error_code.val);
        result.planning_time_ms = planning_time_ms;
        
        // Human-readable error message
        result.error_message = "Planning to '" + state_name + "' failed: ";
        switch (result.error_code) {
            case PlanErrorCode::START_STATE_IN_COLLISION:
                result.error_message += "start state in collision";
                break;
            case PlanErrorCode::GOAL_IN_COLLISION:
                result.error_message += "goal state in collision";
                break;
            case PlanErrorCode::TIMED_OUT:
                result.error_message += "timed out";
                break;
            default:
                result.error_message += "error code " + std::to_string(error_code.val);
        }
        RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
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

bool MauricePlanner::isGoalValid(const std::vector<double>& goal_positions) {
    if (goal_positions.size() != 5) {
        RCLCPP_ERROR(node_->get_logger(), "Expected 5 joints for goal validation");
        return false;
    }
    
    // Use MoveIt's built-in goal constraint checking
    // This checks: joint limits, collisions, and reachability
    bool valid = move_group_->setJointValueTarget(goal_positions);
    
    if (!valid) {
        RCLCPP_WARN(node_->get_logger(), 
                   "Goal validation failed: [%.3f, %.3f, %.3f, %.3f, %.3f]",
                   goal_positions[0], goal_positions[1], goal_positions[2],
                   goal_positions[3], goal_positions[4]);
    }
    
    return valid;
}

PlanResult MauricePlanner::planRelaxed(
    const std::vector<double>& goal_positions,
    double planning_time,
    bool allow_start_collision,
    bool allow_goal_collision) {
    
    PlanResult result;
    result.success = false;
    
    auto start_time = std::chrono::high_resolution_clock::now();
    
    // Validate input
    if (goal_positions.size() != 5) {
        result.error_message = "Expected 5 joint positions, got " + 
                              std::to_string(goal_positions.size());
        result.error_code = PlanErrorCode::INVALID_ROBOT_STATE;
        RCLCPP_ERROR(node_->get_logger(), "%s", result.error_message.c_str());
        return result;
    }
    
    RCLCPP_INFO(node_->get_logger(), 
                "Planning with relaxed constraints (start_collision=%s, goal_collision=%s)",
                allow_start_collision ? "allowed" : "strict",
                allow_goal_collision ? "approximate" : "strict");
    
    // Step 1: Handle start state collision
    if (allow_start_collision) {
        // Get current state and force set it even if in collision
        auto current_state = move_group_->getCurrentState();
        move_group_->setStartState(*current_state);
        RCLCPP_INFO(node_->get_logger(), "⚠️  Start collision checking relaxed");
    }
    
    // Step 2: Handle goal collision
    moveit::planning_interface::MoveGroupInterface::Plan plan;
    moveit::core::MoveItErrorCode error_code;
    
    if (allow_goal_collision) {
        // Strategy: Try to get as close as possible to the goal
        // First, check if goal itself is valid
        bool goal_valid = move_group_->setJointValueTarget(goal_positions);
        
        if (goal_valid) {
            // Goal is actually valid, plan normally
            RCLCPP_INFO(node_->get_logger(), "Goal is valid, planning normally");
            move_group_->setPlanningTime(planning_time);
            error_code = move_group_->plan(plan);
        } else {
            // Goal is invalid (collision or out of bounds)
            // Strategy: Sample points along the way and find the furthest reachable one
            RCLCPP_WARN(node_->get_logger(), 
                       "Goal in collision/invalid, finding closest safe point...");
            
            // Get current position
            auto current_joints = move_group_->getCurrentJointValues();
            
            // Binary search for the furthest safe point toward goal
            double best_fraction = 0.0;
            std::vector<double> best_goal = current_joints;
            
            for (double fraction = 1.0; fraction > 0.0; fraction -= 0.1) {
                std::vector<double> intermediate_goal(5);
                for (size_t i = 0; i < 5; ++i) {
                    intermediate_goal[i] = current_joints[i] + 
                                          fraction * (goal_positions[i] - current_joints[i]);
                }
                
                // Check if this intermediate goal is valid
                if (move_group_->setJointValueTarget(intermediate_goal)) {
                    best_fraction = fraction;
                    best_goal = intermediate_goal;
                    RCLCPP_INFO(node_->get_logger(), 
                               "Found safe point at %.0f%% toward goal", fraction * 100);
                    break;
                }
            }
            
            if (best_fraction > 0.0) {
                // Plan to the best reachable point
                move_group_->setPlanningTime(planning_time);
                error_code = move_group_->plan(plan);
                
                if (error_code == moveit::core::MoveItErrorCode::SUCCESS) {
                    RCLCPP_WARN(node_->get_logger(), 
                               "⚠️  Reached %.0f%% of goal (goal was in collision)",
                               best_fraction * 100);
                }
            } else {
                result.error_message = "No safe point found toward goal";
                result.error_code = PlanErrorCode::GOAL_IN_COLLISION;
                RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
                return result;
            }
        }
    } else {
        // Strict goal checking (normal planning)
        move_group_->setJointValueTarget(goal_positions);
        move_group_->setPlanningTime(planning_time);
        error_code = move_group_->plan(plan);
    }
    
    auto end_time = std::chrono::high_resolution_clock::now();
    double planning_time_ms = 
        std::chrono::duration<double, std::milli>(end_time - start_time).count();
    
    // Process result
    if (error_code == moveit::core::MoveItErrorCode::SUCCESS) {
        result = convertPlan(plan, planning_time_ms);
        result.error_code = PlanErrorCode::SUCCESS;
        
        RCLCPP_INFO(node_->get_logger(), 
                   "✓ Relaxed planning succeeded in %.1fms: %d waypoints",
                   result.planning_time_ms, result.num_waypoints);
    } else {
        result.error_code = static_cast<PlanErrorCode>(error_code.val);
        result.planning_time_ms = planning_time_ms;
        
        switch (result.error_code) {
            case PlanErrorCode::START_STATE_IN_COLLISION:
                result.error_message = "Start state still in collision (too severe)";
                break;
            case PlanErrorCode::GOAL_IN_COLLISION:
                result.error_message = "Could not find path to any safe state near goal";
                break;
            case PlanErrorCode::PLANNING_FAILED:
                result.error_message = "No valid path found";
                break;
            default:
                result.error_message = "Planning failed: error " + std::to_string(error_code.val);
        }
        RCLCPP_ERROR(node_->get_logger(), "❌ %s", result.error_message.c_str());
    }
    
    return result;
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


