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

inline bool isX330(const std::string& motor_type) {
    return motor_type.find("330") != std::string::npos;
}

// ---- Arm reach and self-collision -----------------------------------------
// Link offsets are the joint origins in mars_sim/urdf/mars.urdf. Kept here as
// the single copy: both gain scheduling and the self-collision floor need reach,
// and two transcriptions of the same measurements would drift.
static constexpr double kL2_x = 0.02825, kL2_z = 0.12125;  // joint2 -> joint3
static constexpr double kL3_x = 0.1375, kL3_z = 0.0045;    // joint3 -> joint4
static constexpr double kL45_x = 0.110838;                 // joint4 -> tool
static constexpr double kMaxReach = 0.37291;

// Horizontal distance from the shoulder to the tool. Planar — joint_1 only
// rotates this plane, so it does not appear.
inline double horizReach(double q2, double q3, double q4) {
    const double a2 = q2, a23 = q2 + q3, a234 = q2 + q3 + q4;
    return std::abs(kL2_x * std::cos(a2) + kL2_z * std::sin(a2) + kL3_x * std::cos(a23) +
                    kL3_z * std::sin(a23) + kL45_x * std::cos(a234));
}

// ---- Body keepout ----------------------------------------------------------
// The arm must not intersect the robot's own body, but it MUST be free to reach
// down — over a table edge, say — so the constraint is where the arm is in
// space, not how far a joint has travelled. A floor on joint_2 cannot express
// that: reaching down in front and folding back over the chassis look identical
// to any measure that ignores direction.
//
// So: place the arm's elbow, wrist and tool in the base frame and test them
// against boxes covering the body. Cheap (a handful of AABB tests), and it
// captures the asymmetry — the chassis is rectangular and the shoulder is
// mounted 53 mm off its centreline, which is what makes a corner reachable at
// some joint_1 bearings and not others.
//
// Sampled at three points, so a link can still pass close to a corner between
// samples; the margin covers that. The urdf/FCL check is the version that does
// not sample.

// Shoulder (the joint_2 axis) in base_link, from mars.urdf: joint1 origin
// (0.086, -0.05285, 0.04025) plus joint2's (0, 0, 0.04425).
static constexpr double kShoulderX = 0.086, kShoulderY = -0.05285, kShoulderZ = 0.0845;
// joint4 -> joint6 along the forearm (joint5's 0.019 + joint6's 0.044).
static constexpr double kWristFromElbow = 0.063;

struct BodyBox {
    double min_x, min_y, min_z, max_x, max_y, max_z;
    bool contains(double x, double y, double z, double m) const {
        return x >= min_x - m && x <= max_x + m && y >= min_y - m && y <= max_y + m && z >= min_z - m &&
               z <= max_z + m;
    }
};

struct SelfCollisionConfig {
    std::vector<BodyBox> boxes;
    double margin = 0.015;      // metres of clearance demanded around each box
    int bisect_steps = 8;       // resolution of the walk back toward a safe pose
    bool enabled = true;
    bool valid() const { return !boxes.empty(); }
};

// The arm's elbow, wrist and tool in the shoulder's sagittal plane: `px` along
// the arm's bearing, `pz` vertical. Rotation matches horizReach's convention.
struct ArmPlanarPoints {
    double elbow_x, elbow_z, wrist_x, wrist_z, tool_x, tool_z;
};

inline ArmPlanarPoints armPlanarPoints(double q2, double q3, double q4) {
    const double a23 = q2 + q3, a234 = a23 + q4;
    const double ex = kL2_x * std::cos(q2) + kL2_z * std::sin(q2) + kL3_x * std::cos(a23) + kL3_z * std::sin(a23);
    const double ez = -kL2_x * std::sin(q2) + kL2_z * std::cos(q2) - kL3_x * std::sin(a23) + kL3_z * std::cos(a23);
    const double c = std::cos(a234), s = std::sin(a234);
    return {ex,
            ez,
            ex + kWristFromElbow * c,
            ez - kWristFromElbow * s,
            ex + kL45_x * c,
            ez - kL45_x * s};
}

// True if any sampled point of the arm sits inside a body box. joint_1 rotates
// the sagittal plane about the shoulder, so a planar (px, pz) lifts to
// (shoulder + px·cos q1, shoulder + px·sin q1, shoulder + pz).
inline bool poseHitsBody(double q1, double q2, double q3, double q4, const SelfCollisionConfig& c) {
    if (!c.enabled || !c.valid()) return false;
    const ArmPlanarPoints p = armPlanarPoints(q2, q3, q4);
    const double cq = std::cos(q1), sq = std::sin(q1);
    const double px[3] = {p.elbow_x, p.wrist_x, p.tool_x};
    const double pz[3] = {p.elbow_z, p.wrist_z, p.tool_z};
    for (int i = 0; i < 3; ++i) {
        const double x = kShoulderX + px[i] * cq;
        const double y = kShoulderY + px[i] * sq;
        const double z = kShoulderZ + pz[i];
        for (const auto& b : c.boxes)
            if (b.contains(x, y, z, c.margin)) return true;
    }
    return false;
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
