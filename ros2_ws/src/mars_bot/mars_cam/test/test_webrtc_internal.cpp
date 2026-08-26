// Unit tests for the GStreamer/ROS-free helpers in webrtc_internal.hpp. These are the pieces of the
// streamer that are pure logic (PT/SSRC assignment, ICE/mDNS candidate parsing, status
// rounding) and so are testable without a live pipeline or node. The pipeline/webrtcbin paths stay
// integration-tested by test/webrtc_consumer.py.
#include <gtest/gtest.h>

#include "mars_cam/webrtc_internal.hpp"

using namespace mars_cam;

// ---- RTP payload types: 5-PT stride so each m-line's red/ulpfec/rtx/rtx aux PTs stay unique ----
TEST(CamPt, StrideLeavesAuxSlotsAndAudioClear) {
    EXPECT_EQ(cam_pt_for_index(0), 96);
    EXPECT_EQ(cam_pt_for_index(1), 101);
    EXPECT_EQ(cam_pt_for_index(2), 106);
    for (size_t i = 0; i < kMaxCameras; ++i) {
        EXPECT_LT(cam_pt_for_index(i) + 4, kAudioPt);  // aux block clear of audio
    }
    EXPECT_GE(cam_pt_for_index(kMaxCameras) + 4, kAudioPt);  // one more would collide — the cap is tight
}

// ---- SSRCs: fixed, unique per camera, 1-based off the base ----
TEST(CamSsrc, UniquePerCamera) {
    EXPECT_EQ(cam_ssrc_for_index(0), 0x1A2B3C01u);
    EXPECT_EQ(cam_ssrc_for_index(1), 0x1A2B3C02u);
    EXPECT_NE(cam_ssrc_for_index(0), cam_ssrc_for_index(1));
}

// ---- mDNS detection: browsers obfuscate host candidates as "<uuid>.local" ----
TEST(Mdns, DetectsDotLocalSuffix) {
    EXPECT_TRUE(is_mdns_address("a1b2c3d4.local"));
    EXPECT_FALSE(is_mdns_address("192.168.1.5"));
    EXPECT_FALSE(is_mdns_address("fe80::1"));
    EXPECT_FALSE(is_mdns_address("local"));  // shorter than ".local"
    EXPECT_FALSE(is_mdns_address(""));
}

// ---- candidate_address: pull the 5th whitespace token (the connection address) ----
TEST(CandidateAddress, ExtractsConnectionAddress) {
    // candidate:<foundation> <comp> <proto> <prio> <ADDRESS> <port> typ ...
    EXPECT_EQ(candidate_address("candidate:1 1 UDP 2122260223 192.168.1.5 54321 typ host"), "192.168.1.5");
    EXPECT_EQ(candidate_address("candidate:2 1 UDP 12345 abcd.local 9 typ host"), "abcd.local");
    EXPECT_EQ(candidate_address("candidate:9 1 UDP 1 fe80::abcd 5000 typ host"), "fe80::abcd");
    EXPECT_EQ(candidate_address("too short"), "");  // no 5th token
}

// ---- round1: status fps/rtt rounding to one decimal ----
TEST(Round1, RoundsToOneDecimal) {
    EXPECT_DOUBLE_EQ(round1(1.24), 1.2);
    EXPECT_DOUBLE_EQ(round1(1.27), 1.3);
    EXPECT_DOUBLE_EQ(round1(0.0), 0.0);
    EXPECT_DOUBLE_EQ(round1(14.46), 14.5);
}

// ---- wants: is a camera in this peer's negotiated/active set ----
TEST(Wants, Membership) {
    const std::vector<std::string> v{"main", "arm"};
    EXPECT_TRUE(wants(v, "main"));
    EXPECT_TRUE(wants(v, "arm"));
    EXPECT_FALSE(wants(v, "wrist"));
    EXPECT_FALSE(wants({}, "main"));
}

// ---- conn_state_name: webrtcbin connection-state enum -> status string ----
TEST(ConnStateName, MapsKnownStates) {
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_NEW), "new");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED), "connected");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_FAILED), "failed");
    EXPECT_STREQ(conn_state_name(GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED), "closed");
}

int main(int argc, char** argv) {
    ::testing::InitGoogleTest(&argc, argv);
    return RUN_ALL_TESTS();
}
