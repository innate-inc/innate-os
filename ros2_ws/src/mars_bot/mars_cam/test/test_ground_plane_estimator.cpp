// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for online floor estimation. The behaviour under test is whether a
// small obstacle is still measurable when the camera mount is wrong — which is
// the whole reason this exists.

#include "mars_cam/ground_plane_estimator.hpp"

#include <gtest/gtest.h>

#include <cmath>
#include <random>

using mars_cam::GroundPlaneEstimator;
using mars_cam::GroundPlaneParams;
using Points = std::vector<std::array<double, 3>>;

namespace {

Points floor_points(double pitch_deg = 0.0, double offset_m = 0.0, int n = 1200, double noise_m = 0.002) {
    std::mt19937 rng(5);
    std::uniform_real_distribution<double> ux(0.25, 1.5), uy(-0.5, 0.5);
    std::normal_distribution<double> noise(0.0, noise_m);
    const double slope = std::tan(pitch_deg * M_PI / 180.0);
    Points out;
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        const double x = ux(rng), y = uy(rng);
        out.push_back({x, y, slope * x + offset_m + noise(rng)});
    }
    return out;
}

/// A flat-topped obstacle sitting on the floor plane.
Points obstacle(double x0, double height_m, int n, double pitch_deg = 0.0, double offset_m = 0.0) {
    const double slope = std::tan(pitch_deg * M_PI / 180.0);
    Points out;
    out.reserve(n);
    for (int i = 0; i < n; ++i) {
        const double x = x0 + 0.0005 * i;
        out.push_back({x, 0.02 * ((i % 9) - 4), slope * x + offset_m + height_m});
    }
    return out;
}

Points concat(Points a, const Points& b) {
    a.insert(a.end(), b.begin(), b.end());
    return a;
}

void settle(GroundPlaneEstimator& est, const Points& scene, int frames = 30) {
    for (int i = 0; i < frames; ++i)
        est.update(scene);
}

}  // namespace

TEST(GroundPlaneEstimator, RecoversAFlatFloor) {
    GroundPlaneEstimator est;
    settle(est, floor_points());

    ASSERT_TRUE(est.plane().valid);
    EXPECT_NEAR(est.plane().pitch_deg(), 0.0, 0.1);
    EXPECT_NEAR(est.plane().offset_m, 0.0, 0.003);
}

TEST(GroundPlaneEstimator, RecoversTheMountErrorWeMeasuredOnTheRobot) {
    // R7-27 measured +3.0 deg of tilt and +14mm of height before correction.
    GroundPlaneEstimator est;
    settle(est, floor_points(3.0, 0.014));

    ASSERT_TRUE(est.plane().valid);
    EXPECT_NEAR(est.plane().pitch_deg(), 3.0, 0.1);
    EXPECT_NEAR(est.plane().offset_m, 0.014, 0.003);
}

TEST(GroundPlaneEstimator, ALargeBoxDoesNotTiltTheFloor) {
    // 400 box points against 1200 floor points — a quarter of the view.
    GroundPlaneEstimator est;
    settle(est, concat(floor_points(), obstacle(0.7, 0.22, 400)));

    ASSERT_TRUE(est.plane().valid);
    EXPECT_NEAR(est.plane().pitch_deg(), 0.0, 0.2);
    EXPECT_NEAR(est.plane().offset_m, 0.0, 0.005);
    EXPECT_NEAR(est.plane().height_above(0.7, 0.0, 0.22), 0.22, 0.008);
}

TEST(GroundPlaneEstimator, HonoursTheFifteenMillimetreBenchmark) {
    // The benchmark: 15mm and under can be rolled over, anything above must be
    // flagged. The floor fit must therefore never absorb an 18mm object — with
    // too wide an inlier window it does, and the object measures under its own
    // threshold.
    GroundPlaneEstimator est;
    settle(est, concat(floor_points(), obstacle(0.6, 0.018, 250)));

    ASSERT_TRUE(est.plane().valid);
    EXPECT_GT(est.plane().height_above(0.6, 0.0, 0.018), 0.015) << "18mm must clear the 15mm line";
}

TEST(GroundPlaneEstimator, LeavesRollableClutterBelowTheLine) {
    GroundPlaneEstimator est;
    settle(est, concat(floor_points(), obstacle(0.6, 0.012, 250)));

    ASSERT_TRUE(est.plane().valid);
    EXPECT_LT(est.plane().height_above(0.6, 0.0, 0.012), 0.015) << "12mm is rollable, must not be flagged";
}

TEST(GroundPlaneEstimator, MarginalObjectSurvivesABadMount) {
    // The whole point: a 3 degree mount error and 14mm height offset must not
    // hide an object that is only 2mm over the line.
    const double pitch = 3.0, offset = 0.014;
    const double slope = std::tan(pitch * M_PI / 180.0);
    GroundPlaneEstimator est;
    settle(est, concat(floor_points(pitch, offset), obstacle(0.6, 0.018, 250, pitch, offset)));

    ASSERT_TRUE(est.plane().valid);
    const double z = slope * 0.6 + offset + 0.018;
    EXPECT_GT(est.plane().height_above(0.6, 0.0, z), 0.015);
}

TEST(GroundPlaneEstimator, FindsTheFloorEvenWhenObstaclesOutnumberIt) {
    GroundPlaneEstimator est;
    settle(est, concat(floor_points(0.0, 0.0, 600), obstacle(0.3, 0.15, 900)));

    ASSERT_TRUE(est.plane().valid);
    EXPECT_NEAR(est.plane().offset_m, 0.0, 0.02);
}

TEST(GroundPlaneEstimator, RejectsATiltBeyondThePrior) {
    GroundPlaneEstimator est;
    const auto result = est.update(floor_points(25.0));

    EXPECT_FALSE(result.accepted);
    EXPECT_FALSE(est.plane().valid) << "a wildly tilted surface must not become the floor";
}

TEST(GroundPlaneEstimator, RejectsANonPlanarSurface) {
    std::mt19937 rng(7);
    std::uniform_real_distribution<double> ux(0.25, 1.5), uy(-0.5, 0.5), uz(-0.15, 0.15);
    Points rubble;
    for (int i = 0; i < 1200; ++i)
        rubble.push_back({ux(rng), uy(rng), uz(rng)});

    GroundPlaneEstimator est;
    EXPECT_FALSE(est.update(rubble).accepted);
}

TEST(GroundPlaneEstimator, SurvivesABriefDropoutButFallsBackEventually) {
    GroundPlaneEstimator est;
    settle(est, floor_points(3.0, 0.014));
    ASSERT_TRUE(est.plane().valid);

    for (int i = 0; i < 10; ++i)
        est.update({});
    EXPECT_TRUE(est.plane().valid) << "a few blind frames must not drop the plane";

    for (int i = 0; i < 40; ++i)
        est.update({});
    EXPECT_FALSE(est.plane().valid) << "sustained blindness must fall back to the prior";
}

TEST(GroundPlaneEstimator, FallbackPlaneReproducesTheStaticBehaviour) {
    GroundPlaneEstimator est;
    // With no valid fit, height_above is just z — i.e. base_link z, which is
    // exactly what the pipeline did before this existed.
    EXPECT_FALSE(est.plane().valid);
    EXPECT_DOUBLE_EQ(est.plane().height_above(0.7, 0.1, 0.25), 0.25);
}

TEST(GroundPlaneEstimator, TracksARobotPitchingOverAFloorTransition) {
    // The reported failure: rolling over a bump between two floors pitches the
    // robot several degrees in a few frames. With exponential smoothing the
    // plane lagged that excursion and a flat floor read 93mm high at 1m for 11
    // frames — the whole corridor marked, "the threshold is completely blocked".
    const double profile[] = {0, 2, 4, 6, 5, 3, 0, -3, -5, -6, -4, -2, 0, 0, 0, 0};

    GroundPlaneEstimator est;
    settle(est, floor_points(), 5);

    double worst_m = 0.0;
    for (double robot_pitch : profile) {
        // A flat world floor, seen from a base_link that is itself pitched.
        const double slope = -std::tan(robot_pitch * M_PI / 180.0);
        est.update(floor_points(-robot_pitch));
        const double apparent = std::abs(est.plane().height_above(1.0, 0.0, slope * 1.0));
        worst_m = std::max(worst_m, apparent);
    }

    EXPECT_LT(worst_m, 0.015) << "a flat floor must never read above the flag threshold during a bump";
}

TEST(GroundPlaneEstimator, SlewLimitBluntsACatastrophicFit) {
    // The slew limit is what smoothing was really for: a fit that slipped past
    // the gates must not move the plane arbitrarily far in one frame.
    GroundPlaneParams p;
    p.max_gradient_step = 0.105;
    p.max_offset_step_m = 0.030;
    GroundPlaneEstimator est(p);
    settle(est, floor_points());
    const double before = est.plane().offset_m;

    // A floor abruptly "found" 6cm up — at the very edge of max_offset_m.
    est.update(floor_points(0.0, 0.059));

    EXPECT_LE(std::abs(est.plane().offset_m - before), p.max_offset_step_m + 1e-9);
}
