// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for the temporal evidence filter that sits between stereo and the
// costmap. The behaviour under test is the difference between a robot that
// stutters on single-frame stereo glitches and one that doesn't.

#include "mars_cam/evidence_grid.hpp"

#include <gtest/gtest.h>

using mars_cam::EvidenceGrid;
using mars_cam::EvidenceParams;
using mars_cam::Observation;

namespace {

/// `count` points inside ONE voxel, all with the same confidence and range.
///
/// Callers pass a voxel CENTRE (n*0.05 + 0.025 for the default grid). The
/// jitter is +-1mm so the blob cannot straddle a boundary — with a coordinate
/// sitting on one, the points split across two cells and every lookup checks
/// the wrong one.
std::vector<Observation> blob(float x, float y, float z, float weight, int count, float range = 1.0f) {
    std::vector<Observation> out;
    out.reserve(count);
    for (int i = 0; i < count; ++i) {
        out.push_back({x + 0.001f * static_cast<float>(i % 3 - 1), y, z, weight, range});
    }
    return out;
}

EvidenceParams defaults() {
    EvidenceParams p;
    p.decay_per_second = 1.0;
    return p;
}

}  // namespace

// ---------------------------------------------------------------- the point

TEST(EvidenceGrid, SingleFrameGlitchNeverReachesTheCostmap) {
    EvidenceGrid grid(defaults());

    grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.6f, 10), 0.125);
    EXPECT_TRUE(grid.confirmed().empty()) << "one frame must never be enough at normal range";

    // Glitch gone; a few frames of nothing.
    for (int i = 0; i < 5; ++i)
        grid.integrate({}, 0.125);
    EXPECT_TRUE(grid.confirmed().empty());
    EXPECT_EQ(grid.tracked(), 0u) << "evidence must decay away, not accumulate forever";
}

TEST(EvidenceGrid, ConsistentObstacleIsConfirmedAfterAFewFrames) {
    EvidenceGrid grid(defaults());

    // A solid chair: high confidence, well supported, every frame.
    for (int i = 0; i < 2; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    EXPECT_TRUE(grid.confirmed().empty()) << "two frames is still tentative";

    for (int i = 0; i < 3; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_EQ(grid.confirmed().size(), 1u);
    EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));
}

// ------------------------------------------------------------- confidence

TEST(EvidenceGrid, LowConfidencePointsTakeFarLongerToConfirm) {
    EvidenceGrid confident(defaults());
    EvidenceGrid doubtful(defaults());

    int confident_frames = 0;
    while (confident.confirmed().empty() && confident_frames < 100) {
        confident.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.05);
        ++confident_frames;
    }
    int doubtful_frames = 0;
    while (doubtful.confirmed().empty() && doubtful_frames < 100) {
        doubtful.integrate(blob(1.025f, 0.025f, 0.125f, 0.30f, 40), 0.05);
        ++doubtful_frames;
    }

    EXPECT_LT(confident_frames, doubtful_frames) << "confidence must buy authority, not just presence";
}

TEST(EvidenceGrid, WeightlessObservationsAreIgnoredEntirely) {
    EvidenceGrid grid(defaults());
    for (int i = 0; i < 20; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.0f, 40), 0.125);
    EXPECT_EQ(grid.tracked(), 0u);
}

TEST(RangeConfidence, FallsOffWithDistance) {
    EXPECT_FLOAT_EQ(mars_cam::range_confidence(0.4f, 0.8f, 2.0f), 1.0f);
    EXPECT_FLOAT_EQ(mars_cam::range_confidence(2.5f, 0.8f, 2.0f), 0.0f);
    EXPECT_NEAR(mars_cam::range_confidence(1.4f, 0.8f, 2.0f), 0.5f, 1e-5);
    EXPECT_GT(mars_cam::range_confidence(1.0f, 0.8f, 2.0f), mars_cam::range_confidence(1.8f, 0.8f, 2.0f));
}

// --------------------------------------------------------- spatial support

TEST(EvidenceGrid, SparseClustersAreRejectedNoMatterHowConfident) {
    EvidenceParams p = defaults();
    p.min_points_per_voxel = 4;
    EvidenceGrid grid(p);

    for (int i = 0; i < 50; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 1.0f, 3), 0.125);

    EXPECT_EQ(grid.tracked(), 0u) << "3 points in a voxel is a mismatch, not an object";
}

// --------------------------------------------------------------- near field

TEST(EvidenceGrid, CloseAndConfidentConfirmsOnTheSecondFrame) {
    EvidenceGrid grid(defaults());

    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.9f, 40, /*range=*/0.35f), 0.125);
    EXPECT_FALSE(grid.confirmed_at(0.325f, 0.025f, 0.125f)) << "one frame is a glitch, not a collision";

    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.9f, 40, /*range=*/0.35f), 0.125);
    EXPECT_TRUE(grid.confirmed_at(0.325f, 0.025f, 0.125f)) << "collision safety cannot wait the full four frames";
}

TEST(EvidenceGrid, NearFieldBypassRequiresConsecutiveFrames) {
    EvidenceGrid grid(defaults());

    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.9f, 40, /*range=*/0.35f), 0.125);
    grid.integrate({}, 0.125);
    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.9f, 40, /*range=*/0.35f), 0.125);

    EXPECT_FALSE(grid.confirmed_at(0.325f, 0.025f, 0.125f)) << "two glitches a frame apart are still two glitches";
}

TEST(EvidenceGrid, NearFieldFramesOfOneRestoresTheImmediateBypass) {
    EvidenceParams p = defaults();
    p.near_field_frames = 1;
    EvidenceGrid grid(p);

    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.9f, 40, /*range=*/0.35f), 0.125);

    EXPECT_TRUE(grid.confirmed_at(0.325f, 0.025f, 0.125f));
}

TEST(EvidenceGrid, CloseButUncertainStillWaits) {
    EvidenceGrid grid(defaults());
    grid.integrate(blob(0.325f, 0.025f, 0.125f, 0.4f, 40, /*range=*/0.35f), 0.125);
    EXPECT_FALSE(grid.confirmed_at(0.325f, 0.025f, 0.125f));
}

// --------------------------------------------------------------- hysteresis

TEST(EvidenceGrid, ConfirmedObstacleSurvivesAMissedFrame) {
    EvidenceGrid grid(defaults());
    for (int i = 0; i < 5; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));

    grid.integrate({}, 0.125);  // one dropout
    EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "one missed frame must not un-see a chair";
}

TEST(EvidenceGrid, DoesNotChatterAroundTheMarkThreshold) {
    EvidenceParams p = defaults();
    p.mark_threshold = 3.0;
    p.clear_threshold = 0.5;
    EvidenceGrid grid(p);

    for (int i = 0; i < 5; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));

    // Alternate seen/unseen right at the boundary. With one threshold this
    // toggles every frame; with hysteresis it must stay confirmed.
    for (int i = 0; i < 10; ++i) {
        grid.integrate({}, 0.125);
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
        EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "chattered on iteration " << i;
    }
}

TEST(EvidenceGrid, ObstacleThatTrulyLeavesIsEventuallyCleared) {
    EvidenceParams p = defaults();
    p.decay_per_second = 2.0;
    EvidenceGrid grid(p);

    for (int i = 0; i < 8; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));

    for (int i = 0; i < 40; ++i)
        grid.integrate({}, 0.125);
    EXPECT_FALSE(grid.confirmed_at(1.025f, 0.025f, 0.125f));
    EXPECT_EQ(grid.tracked(), 0u);
}

TEST(EvidenceGrid, ScoreIsCappedSoHistoryCannotBankForever) {
    EvidenceParams p = defaults();
    p.max_score = 6.0;
    EvidenceGrid grid(p);

    for (int i = 0; i < 500; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 1.0f, 40), 0.01);
    EXPECT_LE(grid.score_at(1.025f, 0.025f, 0.125f), p.max_score + 1e-9);
}

// ------------------------------------------------------------- bookkeeping

TEST(EvidenceGrid, SeparateVoxelsAccumulateIndependently) {
    EvidenceGrid grid(defaults());
    for (int i = 0; i < 5; ++i) {
        auto obs = blob(1.025f, 0.025f, 0.125f, 0.95f, 40);
        auto far = blob(1.025f, 0.525f, 0.125f, 0.20f, 40);
        obs.insert(obs.end(), far.begin(), far.end());
        grid.integrate(obs, 0.125);
    }
    EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));
    EXPECT_FALSE(grid.confirmed_at(1.025f, 0.525f, 0.125f));
}

TEST(EvidenceGrid, ConfirmedCentresLandInsideTheirVoxel) {
    EvidenceParams p = defaults();
    p.voxel_size = 0.05;
    EvidenceGrid grid(p);
    for (int i = 0; i < 5; ++i)
        grid.integrate(blob(1.025f, -0.325f, 0.175f, 0.95f, 40), 0.125);

    ASSERT_EQ(grid.confirmed().size(), 1u);
    const auto c = grid.confirmed()[0];
    EXPECT_NEAR(c[0], 1.025f, p.voxel_size);
    EXPECT_NEAR(c[1], -0.325f, p.voxel_size);
    EXPECT_NEAR(c[2], 0.175f, p.voxel_size);
}

TEST(EvidenceGrid, NegativeCoordinatesRoundTripThroughTheKey) {
    EvidenceGrid grid(defaults());
    for (int i = 0; i < 5; ++i)
        grid.integrate(blob(-2.125f, -0.425f, -0.075f, 0.95f, 40), 0.125);

    ASSERT_EQ(grid.confirmed().size(), 1u);
    const auto c = grid.confirmed()[0];
    EXPECT_NEAR(c[0], -2.125f, 0.05f);
    EXPECT_NEAR(c[1], -0.425f, 0.05f);
    EXPECT_NEAR(c[2], -0.075f, 0.05f);
}


// ------------------------------------------------------------------ memory

TEST(EvidenceGrid, RemembersAnObstacleThatLeavesTheCorridor) {
    // The failure this guards: the robot detects something, steers around it,
    // the obstacle slides out of the narrow corridor, evidence evaporates, and
    // the robot runs it over on the final approach it cannot see.
    EvidenceParams p = defaults();
    p.max_score = 10.0;
    p.decay_per_second = 1.0;
    p.clear_threshold = 0.5;
    EvidenceGrid grid(p);

    // ~2.3s of solid observation at 8Hz saturates the score.
    for (int i = 0; i < 20; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));

    // It must survive a normal maneuver out of view.
    for (int i = 0; i < 64; ++i)
        grid.integrate({}, 0.125);
    EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "must survive the maneuver";

    // (max_score - clear_threshold)/decay = 9.5s, so it does eventually go.
    for (int i = 0; i < 24; ++i)
        grid.integrate({}, 0.125);
    EXPECT_FALSE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "must not be remembered forever";
}

TEST(EvidenceGrid, MemoryScalesWithHowWellEstablishedTheObstacleWas) {
    EvidenceParams p = defaults();
    p.max_score = 10.0;
    auto survives_after = [&p](int observed_frames, int blind_frames) {
        EvidenceGrid grid(p);
        for (int i = 0; i < observed_frames; ++i)
            grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
        for (int i = 0; i < blind_frames; ++i)
            grid.integrate({}, 0.125);
        return grid.confirmed_at(1.025f, 0.025f, 0.125f);
    };

    // A glimpse earns less memory than a sustained look — 8s of blindness.
    EXPECT_FALSE(survives_after(/*observed=*/5, /*blind=*/64));
    EXPECT_TRUE(survives_after(/*observed=*/20, /*blind=*/64));
}


// --------------------------------------------------- observed means supported

TEST(EvidenceGrid, ContinuouslyObservedObstacleDoesNotBleedAway) {
    // Observed on the robot: a roll of tape produced ~24 voxels, which then
    // walked down to 0 while it sat in plain view. Cause was decaying every
    // cell each frame and re-adding, so a continuously-seen voxel netted
    // (weight - decay*dt) and drained whenever the weight was small.
    EvidenceParams p = defaults();
    p.decay_per_second = 1.0;
    EvidenceGrid grid(p);

    const float weak = 0.08f;  // well below decay*dt = 0.125
    for (int i = 0; i < 80; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, weak, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "weak but persistent evidence must still accumulate";

    for (int i = 0; i < 200; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, weak, 40), 0.125);
    EXPECT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f)) << "must not bleed away while still in view";
}

TEST(EvidenceGrid, SeenButUnsupportedVoxelsStillDecay) {
    // Looked at, but backed by too few points to corroborate — that is not
    // support, so it must not protect the cell from decay.
    EvidenceParams p = defaults();
    p.min_points_per_voxel = 4;
    EvidenceGrid grid(p);

    for (int i = 0; i < 20; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 40), 0.125);
    ASSERT_TRUE(grid.confirmed_at(1.025f, 0.025f, 0.125f));

    // Now only 2 points per frame land in that voxel.
    for (int i = 0; i < 200; ++i)
        grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.95f, 2), 0.125);
    EXPECT_FALSE(grid.confirmed_at(1.025f, 0.025f, 0.125f));
}

TEST(EvidenceGrid, StatsReportTheFunnel) {
    EvidenceGrid grid(defaults());
    auto s = grid.integrate(blob(1.025f, 0.025f, 0.125f, 0.5f, 40), 0.125);

    EXPECT_EQ(s.observations, 40u);
    EXPECT_EQ(s.voxels_seen, 1u);
    EXPECT_EQ(s.voxels_supported, 1u);
    EXPECT_EQ(s.confirmed, 0u);
    EXPECT_NEAR(s.mean_weight, 0.5, 1e-6);
}
