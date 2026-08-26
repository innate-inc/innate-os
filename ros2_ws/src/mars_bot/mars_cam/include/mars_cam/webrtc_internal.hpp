// Internal helpers shared across the webrtc_streamer translation units (encode / transport / signaling /
// core). Not part of the public node API — these were the file-local (anonymous-namespace) helpers before
// the .cpp was split; they live here, inline, so every split TU sees one definition without duplication.
#pragma once

#include "mars_cam/webrtc_config.hpp"

#include <gst/gst.h>
#include <gst/rtp/rtp.h>
#include <gst/webrtc/webrtc.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

namespace mars_cam {

class WebRTCStreamer;  // OfferContext only holds a pointer

inline GstWebRTCPeerConnectionState peer_connection_state(GstElement* webrtc) {
    GstWebRTCPeerConnectionState state;
    g_object_get(webrtc, "connection-state", &state, nullptr);
    return state;
}

inline const char* conn_state_name(GstWebRTCPeerConnectionState s) {
    switch (s) {
        case GST_WEBRTC_PEER_CONNECTION_STATE_NEW:
            return "new";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTING:
            return "connecting";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED:
            return "connected";
        case GST_WEBRTC_PEER_CONNECTION_STATE_DISCONNECTED:
            return "disconnected";
        case GST_WEBRTC_PEER_CONNECTION_STATE_FAILED:
            return "failed";
        case GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED:
            return "closed";
    }
    return "unknown";
}

inline double round1(double v) {
    return std::round(v * 10.0) / 10.0;
}

inline bool wants(const std::vector<std::string>& videos, const std::string& cam) {
    return std::find(videos.begin(), videos.end(), cam) != videos.end();
}

// Each camera gets a fixed SSRC (base + 1-based index), declared in every peer's transport caps so the
// SDP offer carries a=ssrc/msid (built before any RTP has flowed, so webrtcbin can't infer it). All peers
// share a camera's SSRC — fine, since each peer is an independent SRTP transport.
inline guint cam_ssrc_for_index(size_t index) {
    return 0x1A2B3C00u + static_cast<guint>(index) + 1u;
}

// PTs must be unique across the whole BUNDLE (RFC 8843 §9.1), INCLUDING the red/ulpfec/rtx/rtx PTs
// webrtcbin auto-assigns per video m-line — sequentially after that m-line's media PT, at link time.
// A 5-PT stride (96, 101, 106, …) leaves each m-line its 4 aux slots; audio sits above them all.
inline int cam_pt_for_index(size_t index) {
    return 96 + 5 * static_cast<int>(index);
}

inline constexpr int kAudioPt = 127;

// The dynamic PT range is 96-127; camera 6 would land on 126 and push its aux block onto (and past)
// kAudioPt, so the stride fits exactly 6 cameras. configure_cameras enforces this.
inline constexpr size_t kMaxCameras = 6;

// Chrome/Firefox often obfuscate their host ICE candidates as "<uuid>.local" mDNS names (one per local
// interface). These parsing helpers are kept for diagnostics and for a future non-destructive fast path:
// if we ever resolve a .local name ourselves, we must ADD the resolved-IP candidate, not replace/drop the
// original. Some Linux browsers do not publish their ephemeral mDNS names, so destructive filtering leaves
// the robot with no host candidate at all and prevents peer-reflexive discovery from rescuing the link.
inline std::string candidate_address(const std::string& cand) {
    std::istringstream iss(cand);
    std::string tok;
    for (int idx = 0; iss >> tok; ++idx) {
        if (idx == 4)
            return tok;  // candidate:<foundation> <comp> <proto> <prio> <ADDRESS> <port> typ ...
    }
    return "";
}

inline bool is_mdns_address(const std::string& addr) {
    return addr.size() >= 6 && addr.compare(addr.size() - 6, 6, ".local") == 0;
}

// ---- WebRTC "playout-delay" RTP header extension -----------------------------------------------
// Caps the receiver's de-jitter buffer. GStreamer < 1.24 ships no built-in element for this URI, so we
// implement it as a minimal GstRTPHeaderExtension subclass (defined in webrtc_streamer.cpp) and add it to
// each payloader; the matching a=extmap is emitted by webrtcbin from the transport appsrc caps. Wire
// format (WebRTC experiment): 3 bytes = MIN delay (12 bits) | MAX delay (12 bits), 10 ms units.
#define MARS_PLAYOUT_DELAY_URI "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay"
constexpr guint kPlayoutDelayExtId = 14;

GstRTPHeaderExtension* make_playout_delay_ext(guint ext_id, guint min_ms, guint max_ms);

// Context handed to the async create-offer callback. Holds a ref to the peer's webrtcbin (so it
// survives a concurrent teardown) and a copy of the peer's generation token (a shared_ptr, so it stays
// readable even after the Peer is freed): if the peer was torn down/replaced, the generation no longer
// matches and the stale offer is dropped instead of being applied to a vanished connection.
struct OfferContext {
    WebRTCStreamer* self;
    GstElement* webrtc;  // owns a ref, released in offer_context_free
    std::shared_ptr<std::atomic<uint64_t>> gen;
    uint64_t gen_value;
    std::string client_id;
    guint expected_videos = 0;
    bool expected_audio = false;
};

inline void offer_context_free(gpointer data) {
    auto* ctx = static_cast<OfferContext*>(data);
    if (ctx->webrtc) {
        gst_object_unref(ctx->webrtc);
    }
    delete ctx;
}

}  // namespace mars_cam
