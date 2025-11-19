#ifndef MAURICE_ARM_PLANNER_HPP
#define MAURICE_ARM_PLANNER_HPP

#include <rclcpp/rclcpp.hpp>
#include <moveit/move_group_interface/move_group_interface.h>
#include <moveit/planning_scene_interface/planning_scene_interface.h>
#include <moveit/robot_model_loader/robot_model_loader.h>
#include <moveit/robot_state/robot_state.h>
#include <vector>
#include <string>
#include <memory>

namespace maurice_arm {

/**
 * @brief Error codes for planning failures
 */
enum class PlanErrorCode {
    SUCCESS = 0,
    PLANNING_FAILED = -1,
    INVALID_MOTION_PLAN = -2,
    MOTION_PLAN_INVALIDATED_BY_ENVIRONMENT_CHANGE = -3,
    CONTROL_FAILED = -4,
    UNABLE_TO_AQUIRE_SENSOR_DATA = -5,
    TIMED_OUT = -6,
    PREEMPTED = -7,
    START_STATE_IN_COLLISION = -10,
    START_STATE_VIOLATES_PATH_CONSTRAINTS = -11,
    GOAL_IN_COLLISION = -12,
    GOAL_VIOLATES_PATH_CONSTRAINTS = -13,
    GOAL_CONSTRAINTS_VIOLATED = -14,
    INVALID_GROUP_NAME = -15,
    INVALID_GOAL_CONSTRAINTS = -16,
    INVALID_ROBOT_STATE = -17,
    INVALID_LINK_NAME = -18,
    INVALID_OBJECT_NAME = -19,
    FRAME_TRANSFORM_FAILURE = -20,
    COLLISION_CHECKING_UNAVAILABLE = -21,
    ROBOT_STATE_STALE = -22,
    SENSOR_INFO_STALE = -23,
    COMMUNICATION_FAILURE = -24,
    CRASH = -25,
    ABORT = -26,
    UNKNOWN = -999
};

/**
 * @brief Result of a motion planning query
 */
struct PlanResult {
    bool success;
    std::string error_message;
    PlanErrorCode error_code;                    // Detailed error code
    std::vector<std::vector<double>> waypoints;  // Joint positions for each waypoint
    std::vector<double> time_from_start;         // Time (seconds) for each waypoint
    double planning_time_ms;                     // How long planning took
    int num_waypoints;                           // Number of waypoints in trajectory
};

/**
 * @brief Simple MoveIt planner for Maurice arm
 * 
 * Usage from arm.cpp:
 *   auto planner = std::make_shared<MauricePlanner>(node);
 *   std::vector<double> goal = {0, 0.5, -0.3, 0, 0};
 *   auto result = planner->planToGoal(goal);
 *   if (result.success) {
 *       // Execute trajectory: result.waypoints
 *   }
 */
class MauricePlanner {
public:
    /**
     * @brief Constructor
     * @param node ROS2 node (needed for MoveIt interfaces)
     */
    explicit MauricePlanner(const rclcpp::Node::SharedPtr& node);
    
    ~MauricePlanner() = default;
    
    /**
     * @brief Plan collision-free motion to joint goal
     * @param goal_positions Target joint positions (5 joints: joint1-5 in radians)
     * @param planning_time Maximum time to spend planning (seconds)
     * @return PlanResult with success status and trajectory
     */
    PlanResult planToGoal(
        const std::vector<double>& goal_positions,
        double planning_time = 5.0);
    
    /**
     * @brief Plan with relaxed collision constraints
     * Allows planning FROM collision states (useful if robot is stuck)
     * and TO approximate goals (gets as close as possible if goal in collision)
     * 
     * @param goal_positions Target joint positions (5 joints)
     * @param planning_time Maximum planning time (seconds)
     * @param allow_start_collision If true, ignores collision at start state
     * @param allow_goal_collision If true, gets as close to goal as possible
     * @return PlanResult with trajectory (may not reach exact goal)
     */
    PlanResult planRelaxed(
        const std::vector<double>& goal_positions,
        double planning_time = 5.0,
        bool allow_start_collision = true,
        bool allow_goal_collision = true);
    
    /**
     * @brief Plan to a named state (e.g., "home")
     * @param state_name Named state from SRDF
     * @param planning_time Maximum time to spend planning (seconds)
     * @return PlanResult with success status and trajectory
     */
    PlanResult planToNamedTarget(
        const std::string& state_name,
        double planning_time = 5.0);
    
    /**
     * @brief Check if a configuration is in collision
     * @param joint_positions Joint configuration to check (5 joints)
     * @return true if in collision, false if collision-free
     */
    bool isInCollision(const std::vector<double>& joint_positions);
    
    /**
     * @brief Check if a goal is valid before attempting to plan
     * Validates: joint limits, collisions, reachability
     * @param goal_positions Goal joint positions to validate (5 joints)
     * @return true if goal is valid and planning should succeed, false otherwise
     */
    bool isGoalValid(const std::vector<double>& goal_positions);
    
    /**
     * @brief Get current joint positions from move_group
     * @return Current joint positions (5 joints)
     */
    std::vector<double> getCurrentJointPositions();
    
    /**
     * @brief Update the current state that MoveIt uses for planning
     * Call this before planning to ensure MoveIt knows where the arm is
     * @param current_positions Current joint positions (5 joints)
     */
    void updateCurrentState(const std::vector<double>& current_positions);

private:
    rclcpp::Node::SharedPtr node_;
    std::shared_ptr<moveit::planning_interface::MoveGroupInterface> move_group_;
    std::shared_ptr<moveit::planning_interface::PlanningSceneInterface> planning_scene_;
    
    /**
     * @brief Add ground plane collision object to planning scene
     * Adds a 10m x 10m x 2m box below z=0 to prevent underground planning
     */
    void addGroundPlane();
    
    // Convert MoveIt plan to simple trajectory format
    PlanResult convertPlan(
        const moveit::planning_interface::MoveGroupInterface::Plan& plan,
        double planning_time_ms);
};

} // namespace maurice_arm

#endif // MAURICE_ARM_PLANNER_HPP


