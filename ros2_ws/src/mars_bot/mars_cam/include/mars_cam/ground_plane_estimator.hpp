// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

// Online estimation of the floor the robot is actually standing on.
//
// Obstacle height was previously a point's base_link z, which is only right if
// the camera->base_link transform is right. It is not: the URDF places the
// camera from nominal CAD, the head servo zero is one hardcoded constant shared
// across robots, and the measured floor tilt drifts ~0.027 deg per degree of
// head movement. Every one of those lands as false obstacles.
//
// Measuring height from the OBSERVED floor sidesteps all of it. A plane fit
// recovers exactly the three degrees of freedom that are broken — pitch, roll
// and height — and leaves yaw/x/y to TF, which is not what leaks. It also
// self-corrects for gravity sag, per-unit variation and head angle, and on a
// ramp it tracks the surface the robot is actually driving on.
//
// Header-only and free of ROS, OpenCV and VPI so it unit tests anywhere.
namespace mars_cam {

struct GroundPlaneParams {
    // Candidates are taken from within this band of the prior floor. Wide
    // enough to capture a badly-mounted camera, narrow enough to exclude
    // table tops and most clutter.
    double search_band_m{0.25};
    // Gates. A fit that fails any of these is rejected rather than smoothed in
    // — the failure mode to avoid is fitting a mattress or a wall and then
    // measuring every real obstacle relative to it.
    double max_tilt_deg{12.0};
    double max_offset_m{0.10};
    std::size_t min_inliers{150};
    double max_residual_m{0.02};
    // The floor is the LOWEST surface, so the fit is seeded from the lowest
    // fraction of points in each range bin. Symmetric trimming cannot recover
    // here: a box occupying a quarter of the view drags the first fit far
    // enough that even the median residual moves with it.
    int range_bins{8};
    double seed_fraction{0.30};
    // Inlier window around the seed plane. It MUST stay well below the flag
    // threshold (nav_roi.z_min, 10mm) or a low object is absorbed into the
    // floor and lifts it. Measured floor residual is 1.4mm RMS, so 6mm is 4
    // sigma of headroom while leaving a 4mm margin under the threshold.
    double trim_min_m{0.003};
    double trim_max_m{0.006};
    // The mount moves slowly; measured frame-to-frame jitter is 0.04-0.18 deg,
    // so heavy smoothing costs nothing and keeps obstacle heights steady.
    double smoothing{0.15};
    // After this many consecutive rejections the last good plane is abandoned
    // and the caller falls back to the prior. Facing a wall must not leave a
    // stale plane in force indefinitely.
    int max_consecutive_rejects{25};
};

struct GroundPlane {
    double gradient_x{0.0};
    double gradient_y{0.0};
    double offset_m{0.0};
    bool valid{false};

    /// Height of a point above this plane — the quantity obstacle thresholds
    /// should actually be applied to.
    double height_above(double x, double y, double z) const {
        return z - (gradient_x * x + gradient_y * y + offset_m);
    }

    double pitch_deg() const { return std::atan(gradient_x) * 180.0 / M_PI; }
    double roll_deg() const { return std::atan(gradient_y) * 180.0 / M_PI; }
};

struct GroundFitResult {
    GroundPlane plane;
    bool accepted{false};
    std::size_t candidates{0};
    std::size_t inliers{0};
    double residual_rms_m{0.0};
    const char* rejection{""};
};

class GroundPlaneEstimator {
   public:
    explicit GroundPlaneEstimator(GroundPlaneParams params = {}) : params_(params) {}

    void set_params(const GroundPlaneParams& params) { params_ = params; }

    /// Fit the floor from points already expressed in the prior's frame
    /// (base_link, so the prior plane is z = 0).
    GroundFitResult update(const std::vector<std::array<double, 3>>& points) {
        GroundFitResult result;

        std::vector<std::array<double, 3>> candidates;
        candidates.reserve(points.size());
        for (const auto& p : points) {
            // Measure against the plane in force, not against z=0 — otherwise a
            // genuinely tilted mount walks its own candidates out of the band.
            const double h = current_.valid ? current_.height_above(p[0], p[1], p[2]) : p[2];
            if (std::abs(h) <= params_.search_band_m)
                candidates.push_back(p);
        }
        result.candidates = candidates.size();

        if (candidates.size() < params_.min_inliers) {
            result.rejection = "too few candidates";
            return reject(result);
        }

        GroundPlane fit;
        if (!seeded_fit(candidates, fit)) {
            result.rejection = "no seed";
            return reject(result);
        }
        {
            // Symmetric window around the seed so the final fit is unbiased —
            // seeding from the lowest points alone would sit ~1 sigma low.
            const double window = std::min(std::max(3.0 * residual_spread(candidates, fit), params_.trim_min_m),
                                           params_.trim_max_m);
            std::vector<std::array<double, 3>> inliers;
            inliers.reserve(candidates.size());
            for (const auto& p : candidates) {
                if (std::abs(fit.height_above(p[0], p[1], p[2])) <= window)
                    inliers.push_back(p);
            }
            if (inliers.size() >= params_.min_inliers) {
                candidates.swap(inliers);
                fit = least_squares(candidates);
            }
        }

        result.inliers = candidates.size();
        result.residual_rms_m = residual_rms(candidates, fit);
        result.plane = fit;

        if (candidates.size() < params_.min_inliers) {
            result.rejection = "too few inliers";
            return reject(result);
        }
        if (std::abs(fit.pitch_deg()) > params_.max_tilt_deg || std::abs(fit.roll_deg()) > params_.max_tilt_deg) {
            result.rejection = "tilt beyond prior";
            return reject(result);
        }
        if (std::abs(fit.offset_m) > params_.max_offset_m) {
            result.rejection = "offset beyond prior";
            return reject(result);
        }
        if (result.residual_rms_m > params_.max_residual_m) {
            result.rejection = "surface not planar";
            return reject(result);
        }

        rejects_ = 0;
        const double a = current_.valid ? params_.smoothing : 1.0;
        current_.gradient_x = a * fit.gradient_x + (1.0 - a) * current_.gradient_x;
        current_.gradient_y = a * fit.gradient_y + (1.0 - a) * current_.gradient_y;
        current_.offset_m = a * fit.offset_m + (1.0 - a) * current_.offset_m;
        current_.valid = true;

        result.accepted = true;
        result.plane = current_;
        return result;
    }

    /// The plane to measure against. Falls back to the prior (z = 0) when no
    /// fit has succeeded recently, which reproduces the static behaviour.
    const GroundPlane& plane() const { return current_; }

    int consecutive_rejects() const { return rejects_; }

    void reset() {
        current_ = GroundPlane{};
        rejects_ = 0;
    }

   private:
    GroundFitResult& reject(GroundFitResult& result) {
        if (++rejects_ >= params_.max_consecutive_rejects)
            current_ = GroundPlane{};
        result.plane = current_;
        return result;
    }

    /// Seed the plane from the lowest `seed_fraction` of points in each range
    /// bin. Obstacles only ever raise z, so the low tail of each bin is floor
    /// no matter what is standing on it — and binning by range keeps the seed
    /// spread along x so it constrains tilt, not just height.
    bool seeded_fit(const std::vector<std::array<double, 3>>& candidates, GroundPlane& out) const {
        double x_lo = candidates.front()[0];
        double x_hi = x_lo;
        for (const auto& p : candidates) {
            x_lo = std::min(x_lo, p[0]);
            x_hi = std::max(x_hi, p[0]);
        }
        const int bins = std::max(1, params_.range_bins);
        const double width = std::max(1e-6, (x_hi - x_lo) / bins);

        std::vector<std::vector<std::array<double, 3>>> binned(bins);
        for (const auto& p : candidates) {
            const int i = std::min(bins - 1, std::max(0, static_cast<int>((p[0] - x_lo) / width)));
            binned[i].push_back(p);
        }

        std::vector<std::array<double, 3>> seed;
        seed.reserve(candidates.size() / 2);
        for (auto& bin : binned) {
            if (bin.size() < 4)
                continue;
            std::sort(bin.begin(), bin.end(),
                      [](const std::array<double, 3>& a, const std::array<double, 3>& b) { return a[2] < b[2]; });
            const std::size_t take =
                std::max<std::size_t>(2, static_cast<std::size_t>(bin.size() * params_.seed_fraction));
            seed.insert(seed.end(), bin.begin(), bin.begin() + std::min(take, bin.size()));
        }
        if (seed.size() < 12)
            return false;
        out = least_squares(seed);
        return true;
    }

    static GroundPlane least_squares(const std::vector<std::array<double, 3>>& pts) {
        // Normal equations for z = a*x + b*y + c.
        double sx = 0, sy = 0, sz = 0, sxx = 0, sxy = 0, syy = 0, sxz = 0, syz = 0;
        const double n = static_cast<double>(pts.size());
        for (const auto& p : pts) {
            sx += p[0];
            sy += p[1];
            sz += p[2];
            sxx += p[0] * p[0];
            sxy += p[0] * p[1];
            syy += p[1] * p[1];
            sxz += p[0] * p[2];
            syz += p[1] * p[2];
        }
        const double m00 = sxx, m01 = sxy, m02 = sx;
        const double m11 = syy, m12 = sy;
        const double det = m00 * (m11 * n - m12 * m12) - m01 * (m01 * n - m12 * m02) + m02 * (m01 * m12 - m11 * m02);
        GroundPlane plane;
        if (std::abs(det) < 1e-12) {
            plane.offset_m = n > 0 ? sz / n : 0.0;
            return plane;
        }
        const double i00 = (m11 * n - m12 * m12) / det;
        const double i01 = -(m01 * n - m12 * m02) / det;
        const double i02 = (m01 * m12 - m11 * m02) / det;
        const double i11 = (m00 * n - m02 * m02) / det;
        const double i12 = -(m00 * m12 - m01 * m02) / det;
        const double i22 = (m00 * m11 - m01 * m01) / det;
        plane.gradient_x = i00 * sxz + i01 * syz + i02 * sz;
        plane.gradient_y = i01 * sxz + i11 * syz + i12 * sz;
        plane.offset_m = i02 * sxz + i12 * syz + i22 * sz;
        return plane;
    }

    static double residual_rms(const std::vector<std::array<double, 3>>& pts, const GroundPlane& plane) {
        if (pts.empty())
            return 0.0;
        double sum = 0.0;
        for (const auto& p : pts) {
            const double r = plane.height_above(p[0], p[1], p[2]);
            sum += r * r;
        }
        return std::sqrt(sum / static_cast<double>(pts.size()));
    }

    /// Median absolute deviation, not RMS.
    ///
    /// RMS is inflated by the very outliers the trim is meant to remove: with a
    /// box occupying a quarter of the view, the threshold widens enough to keep
    /// the box and the fit stays dragged. MAD is bounded by the inlier
    /// population no matter how extreme the outliers are.
    static double residual_spread(const std::vector<std::array<double, 3>>& pts, const GroundPlane& plane) {
        if (pts.empty())
            return 0.0;
        std::vector<double> deviations;
        deviations.reserve(pts.size());
        for (const auto& p : pts)
            deviations.push_back(std::abs(plane.height_above(p[0], p[1], p[2])));
        const std::size_t mid = deviations.size() / 2;
        std::nth_element(deviations.begin(), deviations.begin() + mid, deviations.end());
        // 1.4826 makes MAD a consistent estimator of sigma for gaussian noise.
        return 1.4826 * deviations[mid];
    }

    GroundPlaneParams params_;
    GroundPlane current_;
    int rejects_{0};
};

}  // namespace mars_cam
