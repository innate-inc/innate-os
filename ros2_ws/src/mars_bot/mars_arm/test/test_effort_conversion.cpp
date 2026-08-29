// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

#include <gtest/gtest.h>

#include "mars_arm/arm_types.hpp"

namespace mars_arm {

TEST(EffortConversion, UsesMotorFeedbackSemantics) {
    JointConfig x430{};
    x430.motor_type = "XC430-W150";
    EXPECT_DOUBLE_EQ(effortPercent(425, x430), 42.5);
    EXPECT_DOUBLE_EQ(effortPercent(-425, x430), -42.5);

    JointConfig x330{};
    x330.motor_type = "XC330-M288";
    x330.current_limit = 1000;
    EXPECT_DOUBLE_EQ(effortPercent(425, x330), 42.5);
    EXPECT_DOUBLE_EQ(effortPercent(-425, x330), -42.5);
}

TEST(EffortConversion, FallsBackToHardwareCurrentCapacity) {
    JointConfig x330{};
    x330.motor_type = "XC330-M288";
    EXPECT_DOUBLE_EQ(effortPercent(875, x330), 50.0);
}

}  // namespace mars_arm
