// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <string>
#include <vector>
#include <array>
#include <cmath>
#include <algorithm>
#include <cstdint>
#include <chrono>

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
// Rest fold. The guard trips when an arm joint sits this far behind the
// command the control loop wrote for it (position, not load: lifting the arm
// off the floor loads the shoulder like a light obstacle would, but a servo
// that can lift never lags) for this many consecutive waypoints (10 ms each
// at the configured 100 Hz).
// Unobstructed folds from the floor peak just under 0.10 rad on hardware. A
// joint is guarded once it has tracked within the limit, or after the lock-on
// timeout: a limp shoulder falls ~0.5 rad past its software limit and needs a
// moment at profile speed to close that gap.
// An arm left lying on the floor this long with no command of any kind
// (streamed target, trajectory, service) folds itself. Longer than any gap
// between the commands a skill sends while its gripper is at the floor, and
// a service resets it, so a skill recovering a tripped servo keeps the arm.
static constexpr double kRestWhenIdleS = 5.0;
static constexpr double kRestFoldDurationS = 3.0;
static constexpr double kRestContactErrorRad = 0.20;
static constexpr int kContactStrikes = 5;
static constexpr double kContactLockOnTimeoutS = 1.0;
static constexpr double kAtRestRad = 0.05;
// The floor is ~5.5 cm below the shoulder joint on MARS (a collapsed tip
// measures -5 to -6 cm) and the rest pose keeps wrist and tip ~2 cm above
// it, so a wrist or tip below this is lying on the floor. Loop carpet hooks
// the fingertips the moment they slide, so the tip goes up before anything
// moves along the floor. A gripper lying nearly flat pivots up with
// the whole arm about the shoulder: its wrist is on the floor and cannot
// lift it (the wrist servo pulled 1.3 A trying). One pointing down
// moderately levels about the wrist, which then carries only the gripper. A
// steep one lifts as is. Then the shoulder and elbow raise the wrist (forearm
// level, ~10 cm above the shoulder), slower than the fold: raising an
// extended arm is the heaviest move here, and at 1.5 s the shoulder fell
// 0.22 rad behind at a quarter of its torque.
static constexpr double kOnFloorM = -0.02;
static constexpr double kFlatGripperRad = 0.3;
static constexpr double kWristLevelMaxPitchRad = 0.785;
static constexpr double kRestLevelDurationS = 1.0;
static constexpr double kLiftShoulderRad = -0.9;
static constexpr double kLiftElbowRad = 0.9;
static constexpr double kRestLiftDurationS = 2.5;
// The shoulder may only swing back past this while the base yaw is outside
// (kYawRestrictedMin, kYawRestrictedMax); nearer the centre the arm hits the
// body. shoulderMinLimit enforces it on every command, and a fold that starts
// inside the zone holds the shoulder here until the base has yawed clear —
// letting the clamp release it mid-sweep steps the shoulder faster than it
// can follow, which the guard reads as contact.
static constexpr double kShoulderClearanceRad = -0.5;
static constexpr double kYawRestrictedMin = -1.35;
static constexpr double kYawRestrictedMax = 1.25;
static constexpr double kRestShoulderDurationS = 1.5;
// j1-j5. The gripper (j6) is never guarded or retargeted: a gripping claw's
// standing position error IS the grip force.
static constexpr size_t kArmJoints = 5;

// Joints whose /mars/arm/state sign is the servo's negated (0-based index).
inline bool flippedJoint(size_t joint) {
    return joint == 1 || joint == 2 || joint == 3 || joint == 5;
}

// Wrist and gripper tip in the arm's plane, metres from the shoulder joint
// (x forward, z up), from the upper arm, forearm and wrist-to-tip links.
struct PlanarPoint {
    double x;
    double z;
};
inline PlanarPoint wristPoint(double q2, double q3) {
    constexpr double L2_x = 0.02825, L2_z = 0.12125;
    constexpr double L3_x = 0.1375, L3_z = 0.0045;
    const double a2 = q2, a23 = q2 + q3;
    return {L2_x * std::cos(a2) + L2_z * std::sin(a2) + L3_x * std::cos(a23) + L3_z * std::sin(a23),
            -L2_x * std::sin(a2) + L2_z * std::cos(a2) - L3_x * std::sin(a23) + L3_z * std::cos(a23)};
}
inline PlanarPoint gripperTip(double q2, double q3, double q4) {
    constexpr double L45_x = 0.110838;
    const PlanarPoint wrist = wristPoint(q2, q3);
    const double a234 = q2 + q3 + q4;
    return {wrist.x + L45_x * std::cos(a234), wrist.z - L45_x * std::sin(a234)};
}
inline bool onFloor(double q2, double q3, double q4) {
    return std::min(wristPoint(q2, q3).z, gripperTip(q2, q3, q4).z) <= kOnFloorM;
}

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

// Stops a guarded trajectory at the first joint that meets resistance, when
// torque goes off, or when a streaming command takes the arm over; says why.
struct TrajectoryGuard {
    explicit TrajectoryGuard(double max_error) : max_error_rad(max_error) {}
    double max_error_rad;
    std::chrono::steady_clock::time_point started = std::chrono::steady_clock::now();
    std::array<bool, kArmJoints> locked_on{};
    std::array<int, kArmJoints> strikes{};
    int blocked_joint = -1;  // 0-based; set when a joint met resistance
    std::string stop_reason;
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
