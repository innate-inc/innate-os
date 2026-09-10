// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <unordered_map>
#include <vector>

// Temporal evidence accumulation for stereo obstacle detections.
//
// Answers "should I believe this detection?", which is a different question
// from STVL's "how long should a believed obstacle persist without support?".
// Without this stage a single bad stereo frame becomes a lethal voxel, nav2
// correctly brakes, the next frame clears it, and the robot stutters.
//
// Header-only and free of ROS, OpenCV and VPI so it can be unit tested without
// a Jetson.
namespace mars_cam {

struct EvidenceParams {
    double voxel_size{0.05};
    // Roughly "how many good frames to believe it" — a voxel gains at most
    // ~1.0 per frame, so 3.0 means about three consistent observations.
    double mark_threshold{3.0};
    // Deliberately far below mark_threshold. Equal thresholds chatter
    // occupied/free around noisy boundaries; once a chair is believed, it
    // should take much more than one missed frame to disbelieve it.
    double clear_threshold{0.5};
    double decay_per_second{1.0};
    // A handful of isolated points is a mismatch, not an object.
    int min_points_per_voxel{4};
    // Collision safety: close and confident skips the temporal wait entirely.
    double near_field_range{0.6};
    double near_field_weight{0.75};
    // Bounds how much history one long observation can bank, so a stale
    // obstacle still decays in reasonable time after it leaves.
    double max_score{6.0};
};

struct Observation {
    float x{0.0f};
    float y{0.0f};
    float z{0.0f};
    // Per-point confidence in [0,1]: stereo matching confidence folded with a
    // range term, since a point at 3m from a 5px disparity carries nowhere near
    // the authority of one at 70cm.
    float weight{0.0f};
    float range{0.0f};
};

class EvidenceGrid {
   public:
    explicit EvidenceGrid(EvidenceParams params = {}) : params_(params) {}

    void set_params(const EvidenceParams& params) { params_ = params; }

    /// Decay existing evidence, then fold in one frame of observations.
    void integrate(const std::vector<Observation>& observations, double dt_sec) {
        decay(dt_sec);

        struct Bucket {
            double weight_sum{0.0};
            int count{0};
            float min_range{1e9f};
        };
        std::unordered_map<int64_t, Bucket> frame;
        for (const auto& o : observations) {
            if (!(o.weight > 0.0f) || !std::isfinite(o.x) || !std::isfinite(o.y) || !std::isfinite(o.z))
                continue;
            auto& b = frame[key(o.x, o.y, o.z)];
            b.weight_sum += o.weight;
            b.count += 1;
            b.min_range = std::min(b.min_range, o.range);
        }

        for (const auto& [k, b] : frame) {
            // Spatial support: a voxel backed by few points is unstructured
            // noise, and rejecting it here is cheaper than out-voting it later.
            if (b.count < params_.min_points_per_voxel)
                continue;
            const double mean_weight = b.weight_sum / b.count;

            auto& cell = cells_[k];
            cell.score = std::min(cell.score + mean_weight, params_.max_score);

            const bool near_and_certain =
                b.min_range <= params_.near_field_range && mean_weight >= params_.near_field_weight;
            if (near_and_certain) {
                cell.score = std::max(cell.score, params_.mark_threshold);
                cell.confirmed = true;
            } else if (cell.score >= params_.mark_threshold) {
                cell.confirmed = true;
            }
        }
    }

    /// Centres of every voxel currently believed to hold an obstacle.
    std::vector<std::array<float, 3>> confirmed() const {
        std::vector<std::array<float, 3>> out;
        out.reserve(cells_.size());
        for (const auto& [k, cell] : cells_) {
            if (!cell.confirmed)
                continue;
            out.push_back(centre(k));
        }
        return out;
    }

    size_t tracked() const { return cells_.size(); }

    double score_at(float x, float y, float z) const {
        auto it = cells_.find(key(x, y, z));
        return it == cells_.end() ? 0.0 : it->second.score;
    }

    bool confirmed_at(float x, float y, float z) const {
        auto it = cells_.find(key(x, y, z));
        return it != cells_.end() && it->second.confirmed;
    }

    void clear() { cells_.clear(); }

   private:
    struct Cell {
        double score{0.0};
        bool confirmed{false};
    };

    void decay(double dt_sec) {
        if (dt_sec <= 0.0)
            return;
        const double drop = params_.decay_per_second * dt_sec;
        for (auto it = cells_.begin(); it != cells_.end();) {
            it->second.score -= drop;
            if (it->second.confirmed && it->second.score < params_.clear_threshold)
                it->second.confirmed = false;
            // Only untracked once it can no longer influence anything, so the
            // map stays bounded as the robot drives.
            if (it->second.score <= 0.0)
                it = cells_.erase(it);
            else
                ++it;
        }
    }

    int64_t key(float x, float y, float z) const {
        const auto q = [&](float v) { return static_cast<int64_t>(std::floor(v / params_.voxel_size)); };
        // 21 bits per axis: +-1e6 voxels, far beyond a rolling local costmap.
        return ((q(x) & 0x1FFFFF) << 42) | ((q(y) & 0x1FFFFF) << 21) | (q(z) & 0x1FFFFF);
    }

    std::array<float, 3> centre(int64_t k) const {
        const auto unpack = [](int64_t v) {
            v &= 0x1FFFFF;
            return static_cast<int64_t>(v & 0x100000 ? v | ~0x1FFFFF : v);  // sign-extend 21 bits
        };
        const double s = params_.voxel_size;
        return {static_cast<float>((unpack(k >> 42) + 0.5) * s), static_cast<float>((unpack(k >> 21) + 0.5) * s),
                static_cast<float>((unpack(k) + 0.5) * s)};
    }

    EvidenceParams params_;
    std::unordered_map<int64_t, Cell> cells_;
};

/// Confidence that falls off with range: disparity shrinks as 1/Z, so the same
/// one-pixel matching error is worth Z^2/(f*B) metres of depth.
inline float range_confidence(float range_m, float full_trust_m, float no_trust_m) {
    if (range_m <= full_trust_m)
        return 1.0f;
    if (range_m >= no_trust_m)
        return 0.0f;
    return 1.0f - (range_m - full_trust_m) / (no_trust_m - full_trust_m);
}

}  // namespace mars_cam
