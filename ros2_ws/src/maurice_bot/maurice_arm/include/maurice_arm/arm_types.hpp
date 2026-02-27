#pragma once

#include <string>
#include <vector>
#include <array>
#include <cmath>
#include <algorithm>
#include <chrono>
#include <cstdint>

namespace maurice_arm {

// x330 motors (XL330, XC330) have current control hw (addr 38/102, modes 0/5)
// x430 motors do not. addr 38 max = 1750 mA for all x330.
static constexpr int kX330MaxCurrentLimit = 1750;
static constexpr int kLoadWarningThreshold = 800;   // ~80% load (0.1% units)
static constexpr int kTemperatureWarningC = 70;
static constexpr int kGainScheduleInterval = 20;     // control cycles between updates

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
    bool operator!=(const GainProfile& o) const { return !(*this == o); }
};

// Gain mode: SCHEDULED = interpolate near/far by extension, TELEOP = flat teleop gains
enum class GainMode { SCHEDULED, TELEOP };

inline GainProfile parseGainsArray(const std::vector<int64_t>& arr) {
    constexpr int kMaxGain = 16383;
    GainProfile g;
    if (arr.size() >= 1) g.kp  = std::clamp(static_cast<int>(arr[0]), 0, kMaxGain);
    if (arr.size() >= 2) g.ki  = std::clamp(static_cast<int>(arr[1]), 0, kMaxGain);
    if (arr.size() >= 3) g.kd  = std::clamp(static_cast<int>(arr[2]), 0, kMaxGain);
    if (arr.size() >= 4) g.ff1 = std::clamp(static_cast<int>(arr[3]), 0, kMaxGain);
    if (arr.size() >= 5) g.ff2 = std::clamp(static_cast<int>(arr[4]), 0, kMaxGain);
    return g;
}

// Per-motor stress tracking for overload protection
struct MotorStressTracker {
    double score = 0.0;           // accumulated stress score
    bool in_cooldown = false;     // currently resting?
    std::chrono::steady_clock::time_point cooldown_until;  // when to re-enable
};

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

} // namespace maurice_arm
