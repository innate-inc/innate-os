#include <memory>
#include <rclcpp/rclcpp.hpp>
#include "manipulation/recorder_node.hpp"

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    
    auto node = std::make_shared<manipulation::RecorderNode>();
    
    // Use MultiThreadedExecutor for concurrent callback handling
    rclcpp::executors::MultiThreadedExecutor executor;
    executor.add_node(node);
    
    RCLCPP_INFO(node->get_logger(), "Recorder Node (C++) starting with MultiThreadedExecutor");
    
    try {
        executor.spin();
    } catch (const std::exception& e) {
        RCLCPP_ERROR(node->get_logger(), "Exception: %s", e.what());
    }
    
    RCLCPP_INFO(node->get_logger(), "Recorder Node shutting down.");
    rclcpp::shutdown();
    return 0;
}
