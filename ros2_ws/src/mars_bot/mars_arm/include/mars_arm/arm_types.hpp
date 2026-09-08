// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <string>
#include <vector>
#include <array>
#include <cmath>
#include <algorithm>
#include <cstdint>

namespace mars_arm {

// x330 motors (XL330, XC330) have current control hw (addr 38/102, modes 0/5)
// x430 motors do not. addr 38 max = 1750 mA for all x330.
static constexpr int kX330MaxCurrentLimit = 1750;
static constexpr int kLoadWarningThreshold = 800;  // ~80% load (0.1% units)
static constexpr int kTemperatureWarningC = 70;
static constexpr int kGainScheduleInterval = 20;  // control cycles between updates
// How long scheduled (stiff) gains hold after a trajectory before decaying to
// teleop gains. Long enough to span the gaps between a skill's stepped moves,
// short enough that an idle arm is not held stiff and overheating.
static constexpr double kScheduledHoldTimeoutS = 5.0;
// The decay additionally requires shoulder+elbow present load below this
// (0.1% units, so 100 = 10%): a gain drop under real load sags the arm, and
// that jolt shook a carried object out of the gripper. At the folded rest
// pose — the long-idle case the decay exists for — these loads are ~0.
static constexpr int kDecayMaxLoad = 100;
// Rest fold: how long the fold takes, and the contact guard on it. The guard
// trips when an arm joint lags this far behind the command the control loop
// wrote for it, for this many consecutive 10 ms waypoints — the signature of
// an obstacle in the path (a single bad bus read is not contact). Position
// error rather than load: lifting the arm off the floor loads the shoulder as
// much as a light obstacle does, but a servo that can lift never lags.
// An unobstructed fold from the floor peaks just under 0.10 rad on hardware.
// The guard only arms once every joint has tracked within the limit: a limp
// arm falls past its joint limits, so the first (clamped) command can sit
// 0.5 rad away and the servo needs a moment at profile speed to close that
// gap. A joint that never gets there within the lock-on timeout is blocked.
static constexpr double kRestFoldDurationS = 3.0;
static constexpr double kRestContactErrorRad = 0.20;
static constexpr int kContactStrikes = 5;
static constexpr double kContactLockOnTimeoutS = 1.0;
// A gripper hanging this far below its rest pitch has usually been carrying
// the collapsed arm's weight on its tip (a limp arm settles on it), and the
// wrist servo stalls at its current limit trying to pitch it up under that
// load — measured on hardware, joint 4 at 1.75 A. So the fold first lifts the
// wrist clear with the shoulder and elbow (forearm level, wrist ~10 cm above
// the shoulder, gripper still hanging), then folds.
static constexpr double kHangingWristRad = 0.5;
static constexpr double kLiftShoulderRad = -0.9;
static constexpr double kLiftElbowRad = 0.9;
static constexpr double kRestLiftDurationS = 1.5;

inline bool isX330(const std::string& motor_type) {
    return motor_type.find("330") != std::string::npos;
}

struct JointConfig {
    int servo_id;
    std::string motor_type;  // e.g. "XC330-M288", "XL430-W250" — "330" = has current hw
    double min_pos_rad;
    double max_pos_rad;
    int pwm_limit;
    int current_limit = 0;
    // Mode 5 (current-based position) torque cap, mA. 0 = leave at the servo
    // default, which is near-zero — a mode-5 joint MUST set this or it stalls
    // under its own friction.
    int goal_current = 0;
    int homing_offset = 0;
    int control_mode;
    int kp, ki, kd;
    int ff1 = 0;  // Velocity feedforward gain (addr 78, range 0-16383)
    int ff2 = 0;  // Acceleration feedforward gain (addr 76, range 0-16383)
    int profile_velocity = 0;
    int profile_acceleration = 0;
    // Head-specific fields (for joint 7)
    double head_min_angle_deg = 0.0;
    double head_max_angle_deg = 0.0;
    double head_ai_position_deg = 0.0;
    bool head_direction_reversed = false;
};

// Gain profile: 5 PID+FF values per joint
struct GainProfile {
    int kp = 0, ki = 0, kd = 0, ff1 = 0, ff2 = 0;
    bool operator==(const GainProfile& o) const {
        return kp == o.kp && ki == o.ki && kd == o.kd && ff1 == o.ff1 && ff2 == o.ff2;
    }
    bool operator!=(const GainProfile& o) const {
        return !(*this == o);
    }
};

// Gain mode: SCHEDULED = interpolate near/far by extension, TELEOP = flat teleop gains
enum class GainMode { SCHEDULED, TELEOP };

// Passed to a trajectory to stop it at the first joint that meets resistance;
// filled in with the culprit when it trips. j6 is never guarded: a gripping
// claw's standing position error IS the grip force.
struct ContactGuard {
    double max_error_rad;
    bool locked_on = false;  // every guarded joint has tracked within max_error_rad at least once
    int blocked_joint = -1;  // 0-based; -1 until the guard trips
    double blocked_error_rad = 0.0;
};

struct RestOutcome {
    bool at_rest;
    std::string detail;
};

inline GainProfile parseGainsArray(const std::vector<int64_t>& arr) {
    constexpr int kMaxGain = 16383;
    GainProfile g;
    if (arr.size() >= 1)
        g.kp = std::clamp(static_cast<int>(arr[0]), 0, kMaxGain);
    if (arr.size() >= 2)
        g.ki = std::clamp(static_cast<int>(arr[1]), 0, kMaxGain);
    if (arr.size() >= 3)
        g.kd = std::clamp(static_cast<int>(arr[2]), 0, kMaxGain);
    if (arr.size() >= 4)
        g.ff1 = std::clamp(static_cast<int>(arr[3]), 0, kMaxGain);
    if (arr.size() >= 5)
        g.ff2 = std::clamp(static_cast<int>(arr[4]), 0, kMaxGain);
    return g;
}

struct TimingAccumulator {
    const char* name = "";
    long sum_us = 0;
    long max_us = 0;
    long samples = 0;

    void add(long us) {
        sum_us += us;
        max_us = std::max(max_us, us);
        ++samples;
    }

    long avg() const {
        return samples > 0 ? (sum_us / samples) : 0;
    }
};

}  // namespace mars_arm
