// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
//
// mars_control is the only writer of the robot's hostname, and the provisioning laptop
// waits on the .local name it predicts from the same robot name. These fixtures are the
// contract between the two — identity.py in innate-jetson asserts the same table.

#include <gtest/gtest.h>

#include <string>

#include "../mars_control/hostname_utils.hpp"

TEST(SanitizeHostname, OrdinalRobotNames) {
    EXPECT_EQ(sanitize_hostname("MARS the 41st"), "mars-the-41st");
    EXPECT_EQ(sanitize_hostname("MARS the 1st"), "mars-the-1st");
    EXPECT_EQ(sanitize_hostname("MARS the 13th"), "mars-the-13th");
    EXPECT_EQ(sanitize_hostname("MARS the blue"), "mars-the-blue");
}

TEST(SanitizeHostname, UnprovisionedSerialName) {
    EXPECT_EQ(sanitize_hostname("MARS-A1B2"), "mars-a1b2");
}

TEST(SanitizeHostname, PunctuationRunsCollapse) {
    EXPECT_EQ(sanitize_hostname("R7.1-3"), "r7-1-3");
    EXPECT_EQ(sanitize_hostname("MARS   the    41st"), "mars-the-41st");
}

TEST(SanitizeHostname, CapsAtSixtyThreeWithoutTrailingHyphen) {
    const std::string long_name(90, 'a');
    EXPECT_EQ(sanitize_hostname(long_name).size(), 63u);

    // A name whose 63rd character is the separator must not leave a trailing hyphen.
    const std::string split_name = std::string(62, 'a') + " tail";
    const std::string result = sanitize_hostname(split_name);
    EXPECT_EQ(result.size(), 62u);
    EXPECT_NE(result.back(), '-');
}

TEST(SanitizeHostname, DegenerateNamesFallBackToMars) {
    EXPECT_EQ(sanitize_hostname(""), "mars");
    EXPECT_EQ(sanitize_hostname("!!! ---"), "mars");
    EXPECT_EQ(sanitize_hostname("-MARS-"), "mars");
}
