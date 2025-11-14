#include "maurice_arm/collision_checker.hpp"

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<maurice_arm::CollisionChecker>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}

