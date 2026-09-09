// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <string>
#include <vector>
#include <array>
#include <cmath>
#include <algorithm>
#include <cstdint>
#include <limits>

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
    // Clearance demanded around THIS box. Per-box because one global figure
    // cannot work: the shoulder is bolted 51 mm from the chassis and never
    // moves further away, so any global margin near that blocks the arm at
    // rest. The chassis therefore gets a small pad and the parts the arm
    // actually swings into — the turret and neck above it — get a large one.
    double pad = 0.015;
    bool contains(double x, double y, double z, double m) const {
        return x >= min_x - m && x <= max_x + m && y >= min_y - m && y <= max_y + m && z >= min_z - m &&
               z <= max_z + m;
    }
};

struct SelfCollisionConfig {
    std::vector<BodyBox> boxes;
    double margin = 0.015;       // floor under every box's own pad
    double slow_margin = 0.070;  // soft zone: motion is scaled back inside this
    int bisect_steps = 8;        // resolution of the walk back toward a safe pose
    bool enabled = true;
    bool valid() const { return !boxes.empty(); }
};

// Distance from a point to a box, zero inside it.
inline double pointBoxDistance(const double p[3], const BodyBox& b) {
    const double lo[3] = {b.min_x, b.min_y, b.min_z};
    const double hi[3] = {b.max_x, b.max_y, b.max_z};
    double sum = 0.0;
    for (int i = 0; i < 3; ++i) {
        const double d = std::max({lo[i] - p[i], 0.0, p[i] - hi[i]});
        sum += d * d;
    }
    return std::sqrt(sum);
}

// The arm's joints in the shoulder's sagittal plane: `x` along the arm's
// bearing, `z` vertical. Rotation matches horizReach's convention. Five points,
// shoulder first, so the LINKS between them can be tested — the joints alone all
// sit past 0.2 m and would leave the whole upper arm uncovered.
struct ArmPlanarPoints {
    static constexpr int kCount = 5;
    double x[kCount], z[kCount];  // shoulder, joint3, elbow(joint4), wrist(joint6), tool
};

inline ArmPlanarPoints armPlanarPoints(double q2, double q3, double q4) {
    const double a23 = q2 + q3, a234 = a23 + q4;
    const double c2 = std::cos(q2), s2 = std::sin(q2);
    const double c23 = std::cos(a23), s23 = std::sin(a23);
    const double c234 = std::cos(a234), s234 = std::sin(a234);
    ArmPlanarPoints p{};
    p.x[0] = 0.0;
    p.z[0] = 0.0;
    p.x[1] = kL2_x * c2 + kL2_z * s2;
    p.z[1] = -kL2_x * s2 + kL2_z * c2;
    p.x[2] = p.x[1] + kL3_x * c23 + kL3_z * s23;
    p.z[2] = p.z[1] - kL3_x * s23 + kL3_z * c23;
    p.x[3] = p.x[2] + kWristFromElbow * c234;
    p.z[3] = p.z[2] - kWristFromElbow * s234;
    p.x[4] = p.x[2] + kL45_x * c234;
    p.z[4] = p.z[2] - kL45_x * s234;
    return p;
}

// Segment against an axis-aligned box, by the slab method. Exact for a
// zero-thickness segment; the box's margin stands in for link radius.
inline bool segmentHitsBox(const double a[3], const double b[3], const BodyBox& box, double m) {
    const double lo[3] = {box.min_x - m, box.min_y - m, box.min_z - m};
    const double hi[3] = {box.max_x + m, box.max_y + m, box.max_z + m};
    double t0 = 0.0, t1 = 1.0;
    for (int i = 0; i < 3; ++i) {
        const double d = b[i] - a[i];
        if (std::fabs(d) < 1e-12) {
            if (a[i] < lo[i] || a[i] > hi[i]) return false;  // parallel to the slab, outside it
            continue;
        }
        double tn = (lo[i] - a[i]) / d, tf = (hi[i] - a[i]) / d;
        if (tn > tf) std::swap(tn, tf);
        t0 = std::max(t0, tn);
        t1 = std::min(t1, tf);
        if (t0 > t1) return false;
    }
    return true;
}

// True if any part of any arm link intersects a body box. joint_1 rotates the
// sagittal plane about the shoulder, so a planar (x, z) lifts to
// (shoulder + x·cos q1, shoulder + x·sin q1, shoulder + z).
//
// Whole links, not sampled points: the joints sit at 0.21, 0.26 and 0.30 m, so
// testing only those left the first 0.21 m of arm uncovered and let 56% of real
// collisions through.
inline bool poseHitsBody(double q1, double q2, double q3, double q4, const SelfCollisionConfig& c) {
    if (!c.enabled || !c.valid()) return false;
    const ArmPlanarPoints p = armPlanarPoints(q2, q3, q4);
    const double cq = std::cos(q1), sq = std::sin(q1);
    double pts[ArmPlanarPoints::kCount][3];
    for (int i = 0; i < ArmPlanarPoints::kCount; ++i) {
        pts[i][0] = kShoulderX + p.x[i] * cq;
        pts[i][1] = kShoulderY + p.x[i] * sq;
        pts[i][2] = kShoulderZ + p.z[i];
    }
    for (int i = 0; i + 1 < ArmPlanarPoints::kCount; ++i)
        for (const auto& b : c.boxes)
            if (segmentHitsBox(pts[i], pts[i + 1], b, std::max(c.margin, b.pad))) return true;
    return false;
}

// How much room the arm has left, in metres, before it touches the body.
//
// Sampled along each link rather than solved exactly: this drives the soft zone,
// where being a millimetre out changes only how early the arm eases off. The
// hard stop stays on poseHitsBody's exact slab test, so nothing safety-critical
// rests on the sampling.
inline double bodyClearance(double q1, double q2, double q3, double q4, const SelfCollisionConfig& c) {
    if (!c.enabled || !c.valid()) return std::numeric_limits<double>::infinity();
    const ArmPlanarPoints p = armPlanarPoints(q2, q3, q4);
    const double cq = std::cos(q1), sq = std::sin(q1);
    double pts[ArmPlanarPoints::kCount][3];
    for (int i = 0; i < ArmPlanarPoints::kCount; ++i) {
        pts[i][0] = kShoulderX + p.x[i] * cq;
        pts[i][1] = kShoulderY + p.x[i] * sq;
        pts[i][2] = kShoulderZ + p.z[i];
    }
    constexpr int kSamplesPerLink = 8;
    double best = std::numeric_limits<double>::infinity();
    for (int i = 0; i + 1 < ArmPlanarPoints::kCount; ++i) {
        const double dx = pts[i + 1][0] - pts[i][0], dy = pts[i + 1][1] - pts[i][1],
                     dz = pts[i + 1][2] - pts[i][2];
        double near = std::numeric_limits<double>::infinity();
        for (int k = 0; k <= kSamplesPerLink; ++k) {
            const double t = static_cast<double>(k) / kSamplesPerLink;
            const double q[3] = {pts[i][0] + t * dx, pts[i][1] + t * dy, pts[i][2] + t * dz};
            // Distance to each box less its own pad: a box demanding more room
            // reads as closer, so one clearance number still drives the taper.
            for (const auto& b : c.boxes)
                near = std::min(near, pointBoxDistance(q, b) - (std::max(c.margin, b.pad) - c.margin));
        }
        // The true closest point can sit midway between two samples, so a raw
        // sampled minimum over-reads by up to half the spacing — enough, on these
        // link lengths, to be worth as much as the whole margin. Subtracting it
        // makes the estimate conservative by construction, which is what the
        // taper needs: reading low only eases the arm off early.
        const double half_spacing = 0.5 * std::sqrt(dx * dx + dy * dy + dz * dz) / kSamplesPerLink;
        best = std::min(best, std::max(0.0, near - half_spacing));
    }
    return best;
}

// The fraction of a requested move to accept, given how close the arm is.
// 1 well clear, falling to 0 at the hard margin, so the arm eases off as it
// approaches instead of running at full speed into a wall. The taper is also
// what the leader feels: a scaled-back command diverges from what was asked,
// and that gap is the force the operator gets back.
inline double approachScale(double clearance, const SelfCollisionConfig& c) {
    if (clearance >= c.slow_margin) return 1.0;
    const double span = c.slow_margin - c.margin;
    if (span <= 0.0) return clearance > c.margin ? 1.0 : 0.0;
    return std::clamp((clearance - c.margin) / span, 0.0, 1.0);
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
