#include <memory>
#include <csignal>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp/executors/multi_threaded_executor.hpp"

#include "maurice_nav/mode_manager.hpp"
#include "maurice_nav/navigate_to_pose_router.hpp"

// Global executor pointer for signal handler
std::shared_ptr<rclcpp::executors::MultiThreadedExecutor> g_executor = nullptr;

void signalHandler(int signum)
{
  (void)signum;
  if (g_executor != nullptr) {
    RCLCPP_INFO(rclcpp::get_logger("mode_manager_main"), "Received signal, shutting down...");
    g_executor->cancel();
  }
  rclcpp::shutdown();
}

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);

  // Install signal handlers
  std::signal(SIGINT, signalHandler);
  std::signal(SIGTERM, signalHandler);

  try {
    // Create nodes
    auto mode_manager = std::make_shared<maurice_nav::ModeManager>();
    auto navigate_to_pose_router = std::make_shared<maurice_nav::NavigateToPoseRouter>();

    // Create multi-threaded executor with 4 threads
    g_executor = std::make_shared<rclcpp::executors::MultiThreadedExecutor>(
      rclcpp::ExecutorOptions(), 4);

    // Add nodes to executor
    g_executor->add_node(mode_manager);
    g_executor->add_node(navigate_to_pose_router);

    RCLCPP_INFO(rclcpp::get_logger("mode_manager_main"), "Starting Mode Manager and NavigateToPoseRouter");

    // Spin
    g_executor->spin();

    RCLCPP_INFO(rclcpp::get_logger("mode_manager_main"), "Executor stopped");

  } catch (const std::exception& e) {
    RCLCPP_ERROR(rclcpp::get_logger("mode_manager_main"), "Unexpected error in main: %s", e.what());

    // Attempt graceful shutdown
    try {
      if (g_executor != nullptr) {
        RCLCPP_INFO(rclcpp::get_logger("mode_manager_main"), "Shutting down executor...");
        g_executor->cancel();
      }
    } catch (const std::exception& shutdown_error) {
      RCLCPP_ERROR(rclcpp::get_logger("mode_manager_main"),
        "Error during shutdown: %s", shutdown_error.what());
    }
  }

  // Cleanup
  if (g_executor != nullptr) {
    g_executor.reset();
  }

  rclcpp::shutdown();

  return 0;
}
