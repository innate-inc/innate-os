#pragma once

#include <vector>
#include <memory>
#include "maurice_arm/dynamixel.hpp"
#include "dynamixel_sdk/dynamixel_sdk.h"

namespace maurice_arm {

class Robot {
public:
    Robot(std::shared_ptr<Dynamixel> dynamixel, const std::vector<int>& servo_ids);
    ~Robot();

    std::vector<int> readPosition(int tries = 2);
    std::vector<int> readVelocity();
    std::pair<std::vector<int>, std::vector<int>> readState(int tries = 2);  // Read both position and velocity in one transaction
    void setGoalPos(const std::vector<int>& action);
    void rebootAllServos();

private:
    std::shared_ptr<Dynamixel> dynamixel_;
    std::vector<int> servo_ids_;
    
    std::unique_ptr<dynamixel::GroupSyncRead> position_reader_;
    std::unique_ptr<dynamixel::GroupSyncRead> velocity_reader_;
    std::unique_ptr<dynamixel::GroupSyncRead> state_reader_;  // Combined reader for both velocity and position
    std::unique_ptr<dynamixel::GroupSyncWrite> pos_writer_;
    
    static constexpr int ADDR_PRESENT_POSITION = 132;
    static constexpr int ADDR_PRESENT_VELOCITY = 128;
    static constexpr int ADDR_GOAL_POSITION = 116;
};

} // namespace maurice_arm

