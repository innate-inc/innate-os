#include <gtest/gtest.h>
#include <memory>

#include "rclcpp/rclcpp.hpp"
#include "maurice_nav/lifecycle_service_utils.hpp"
#include "lifecycle_msgs/msg/state.hpp"
#include "lifecycle_msgs/msg/transition.hpp"

class LifecycleServiceUtilsTest : public ::testing::Test
{
protected:
  void SetUp() override
  {
    // Initialize ROS if not already initialized
    if (!rclcpp::ok()) {
      rclcpp::init(0, nullptr);
    }

    // Create a test node
    test_node_ = std::make_shared<rclcpp::Node>("test_lifecycle_service_utils");

    // Create callback group
    callback_group_ = test_node_->create_callback_group(
      rclcpp::CallbackGroupType::Reentrant);

    // Create lifecycle service client
    lifecycle_client_ = std::make_shared<maurice_nav::LifecycleServiceClient>(
      test_node_, callback_group_);
  }

  void TearDown() override
  {
    lifecycle_client_.reset();
    test_node_.reset();
  }

  rclcpp::Node::SharedPtr test_node_;
  rclcpp::CallbackGroup::SharedPtr callback_group_;
  std::shared_ptr<maurice_nav::LifecycleServiceClient> lifecycle_client_;
};

// ========== Service Call Tests ==========

TEST_F(LifecycleServiceUtilsTest, GetNodeState_NonexistentNode)
{
  // Try to get state of a node that doesn't exist
  auto state = lifecycle_client_->getNodeState("nonexistent_node", 1.0);

  // Should return nullopt
  EXPECT_FALSE(state.has_value());
}

TEST_F(LifecycleServiceUtilsTest, SendLifecycleTransition_NonexistentNode)
{
  // Try to send transition to a node that doesn't exist
  bool success = lifecycle_client_->sendLifecycleTransition(
    "nonexistent_node",
    lifecycle_msgs::msg::Transition::TRANSITION_CONFIGURE,
    1.0);

  // Should return false
  EXPECT_FALSE(success);
}

TEST_F(LifecycleServiceUtilsTest, TransitionNode_NonexistentNode)
{
  // Try to transition a node that doesn't exist
  bool success = lifecycle_client_->transitionNode(
    "nonexistent_node",
    lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE,
    false);

  // Should return false
  EXPECT_FALSE(success);
}

// ========== Generic Service Call Tests ==========

TEST_F(LifecycleServiceUtilsTest, CallService_NonexistentService)
{
  // Try to call a service that doesn't exist
  auto request = std::make_shared<lifecycle_msgs::srv::GetState::Request>();

  auto response = lifecycle_client_->callService<lifecycle_msgs::srv::GetState>(
    "/nonexistent_service",
    request,
    1.0);

  // Should return nullopt
  EXPECT_FALSE(response.has_value());
}

// ========== State Validation Tests ==========

TEST_F(LifecycleServiceUtilsTest, StateConstants_AreValid)
{
  // Verify lifecycle state constants are valid
  EXPECT_EQ(lifecycle_msgs::msg::State::PRIMARY_STATE_UNKNOWN, 0);
  EXPECT_EQ(lifecycle_msgs::msg::State::PRIMARY_STATE_UNCONFIGURED, 1);
  EXPECT_EQ(lifecycle_msgs::msg::State::PRIMARY_STATE_INACTIVE, 2);
  EXPECT_EQ(lifecycle_msgs::msg::State::PRIMARY_STATE_ACTIVE, 3);
  EXPECT_EQ(lifecycle_msgs::msg::State::PRIMARY_STATE_FINALIZED, 4);
}

TEST_F(LifecycleServiceUtilsTest, TransitionConstants_AreValid)
{
  // Verify lifecycle transition constants are valid
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_CREATE, 0);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_CONFIGURE, 1);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_CLEANUP, 2);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_ACTIVATE, 3);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_DEACTIVATE, 4);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_UNCONFIGURED_SHUTDOWN, 5);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_INACTIVE_SHUTDOWN, 6);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_ACTIVE_SHUTDOWN, 7);
  EXPECT_EQ(lifecycle_msgs::msg::Transition::TRANSITION_DESTROY, 8);
}

int main(int argc, char** argv)
{
  testing::InitGoogleTest(&argc, argv);
  rclcpp::init(argc, argv);
  int result = RUN_ALL_TESTS();
  rclcpp::shutdown();
  return result;
}
