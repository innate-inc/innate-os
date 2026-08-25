#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <gst/app/app.h>
#include <gst/rtp/rtp.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <memory>
#include <sstream>

namespace mars_cam {

namespace {
constexpr size_t kMaxStartMessageBytes = 8192;
constexpr size_t kMaxSignalMessageBytes = 320 * 1024;
constexpr size_t kMaxSdpBytes = 256 * 1024;
constexpr size_t kMaxIceCandidateBytes = 4096;
constexpr size_t kMaxClientIdBytes = 128;
constexpr size_t kMaxPendingMdnsIce = 64;

struct SdpMediaCounts {
    guint video = 0;
    guint audio = 0;
    guint application = 0;
};

SdpMediaCounts count_sdp_media(const GstSDPMessage* sdp) {
    SdpMediaCounts counts;
    if (!sdp) {
        return counts;
    }
    const guint media_count = gst_sdp_message_medias_len(sdp);
    for (guint i = 0; i < media_count; ++i) {
        const GstSDPMedia* media = gst_sdp_message_get_media(sdp, i);
        const char* kind = media ? gst_sdp_media_get_media(media) : nullptr;
        if (!kind) {
            continue;
        }
        if (g_strcmp0(kind, "video") == 0) {
            counts.video += 1;
        } else if (g_strcmp0(kind, "audio") == 0) {
            counts.audio += 1;
        } else if (g_strcmp0(kind, "application") == 0) {
            counts.application += 1;
        }
    }
    return counts;
}

bool is_safe_client_id(const std::string& id) {
    if (id.empty() || id.size() > kMaxClientIdBytes) {
        return false;
    }
    return std::all_of(id.begin(), id.end(), [](unsigned char c) {
        return std::isalnum(c) || c == '-' || c == '_' || c == '.' || c == ':' || c == '@';
    });
}

bool is_valid_source(const std::string& source) {
    return source == "live" || source == "replay";
}

bool parse_json_object(const std::string& data, size_t max_bytes, nlohmann::json* out) {
    if (data.empty() || data.size() > max_bytes) {
        return false;
    }
    try {
        *out = nlohmann::json::parse(data);
    } catch (const nlohmann::json::exception&) {
        return false;
    }
    return out->is_object();
}

const char* peer_label(const std::string& client_id) {
    return client_id.empty() ? "(missing)" : client_id.c_str();
}
}  // namespace

// =============================================================================
// Signaling
// =============================================================================

void WebRTCStreamer::on_start(const std_msgs::msg::String::SharedPtr msg) {
    std::string source = "live";
    bool request_audio = false;
    bool request_talk = false;
    std::string client_id;
    std::vector<std::string> videos;
    bool video_specified = false;
    bool renegotiate = false;
    nlohmann::json json;
    if (!parse_json_object(msg->data, kMaxStartMessageBytes, &json)) {
        RCLCPP_WARN(this->get_logger(), "Ignoring malformed /webrtc/start message (%zu bytes)", msg->data.size());
        return;
    }
    if (!json.contains("client_id") || !json["client_id"].is_string()) {
        RCLCPP_WARN(this->get_logger(), "Ignoring START without string client_id");
        return;
    }
    client_id = json["client_id"].get<std::string>();
    if (!is_safe_client_id(client_id)) {
        RCLCPP_WARN(this->get_logger(), "Ignoring START with invalid client_id");
        return;
    }
    if (json.contains("source")) {
        if (!json["source"].is_string()) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': source must be a string", client_id.c_str());
            return;
        }
        source = json["source"].get<std::string>();
        if (!is_valid_source(source)) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': invalid source '%s'", client_id.c_str(),
                        source.c_str());
            return;
        }
    }
    if (json.contains("audio")) {
        if (!json["audio"].is_boolean()) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': audio must be boolean", client_id.c_str());
            return;
        }
        request_audio = json["audio"].get<bool>();
    }
    if (json.contains("talk")) {
        if (!json["talk"].is_boolean()) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': talk must be boolean", client_id.c_str());
            return;
        }
        request_talk = json["talk"].get<bool>();
    }
    if (json.contains("renegotiate")) {
        if (!json["renegotiate"].is_boolean()) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': renegotiate must be boolean", client_id.c_str());
            return;
        }
        renegotiate = json["renegotiate"].get<bool>();
    }
    if (json.contains("video")) {
        if (!json["video"].is_array()) {
            RCLCPP_WARN(this->get_logger(), "Ignoring START for '%s': video must be an array", client_id.c_str());
            return;
        }
        video_specified = true;
        for (const auto& e : json["video"]) {
            if (!e.is_string()) {
                RCLCPP_WARN(this->get_logger(), "Ignoring non-string video entry from '%s'", client_id.c_str());
                continue;
            }
            const std::string s = e.get<std::string>();
            if (s.size() > kMaxClientIdBytes) {
                continue;
            }
            if (find_camera(s) && !wants(videos, s)) {
                videos.push_back(s);  // accept any configured camera name
            }
        }
    }
    if (!video_specified) {
        for (auto& cam : cameras_)
            videos.push_back(cam->name);  // default: all configured cameras
    }

    std::string video_list;
    for (const auto& v : videos) {
        video_list += (video_list.empty() ? "" : "+") + v;
    }
    RCLCPP_INFO(this->get_logger(), "START '%s' (source=%s, video=[%s], audio=%s, talk=%s, renegotiate=%s)",
                client_id.c_str(), source.c_str(), video_list.c_str(), request_audio ? "requested" : "off",
                request_talk ? "requested" : "off", renegotiate ? "true" : "false");

    std::lock_guard<std::mutex> lock(peers_mutex_);

    // Source is global across peers; re-point the shared subscriptions if it changed.
    if (source != current_source_) {
        current_source_ = source;
        destroy_subscriptions();    // topics changed; drop the old-source subs...
        reconcile_subscriptions();  // ...and re-subscribe (lazily) whatever current peers still negotiate
        RCLCPP_INFO(this->get_logger(), "Global source switched to %s", source.c_str());
    }

    const bool audio_active = enable_audio_ && request_audio;
    const bool talk_active = enable_talkback_ && request_talk;
    // Peers negotiate the audio m-line up front (if a mic exists) so toggling is reneg-free, like cameras.
    const bool negotiate_audio = enable_audio_;

    // A talk-only peer (cameras off, not listening) still needs its transport — releasing it here would
    // cut the operator off mid-sentence.
    if (videos.empty() && !audio_active && !talk_active) {
        RCLCPP_INFO(this->get_logger(), "START requested no streams; releasing peer");
        destroy_peer(client_id);
        return;
    }

    // Stream/audio switch on an already-set-up independent peer (its negotiated m-lines unchanged): flip
    // which cameras + audio are pushed live, with no renegotiation/ICE — instant, not a reconnect.
    if (!renegotiate) {
        auto it = peers_.find(client_id);
        if (it != peers_.end() && it->second->with_audio == negotiate_audio) {
            update_peer_active(it->second.get(), videos, audio_active, talk_active);
            return;
        }
    }

    // Connect (or audio-negotiation change). Peers negotiate ALL cameras up front so future switches stay
    // reneg-free.
    std::vector<std::string> all_cams;
    for (auto& cam : cameras_)
        all_cams.push_back(cam->name);
    if (!create_peer_transport(client_id, all_cams, videos, negotiate_audio, audio_active, talk_active)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to start transport for peer");
    }
}

void WebRTCStreamer::publish_offer(const std::string& client_id, const std::string& sdp) {
    auto msg = std_msgs::msg::String();
    nlohmann::json j;
    j["client_id"] = client_id;
    j["sdp"] = sdp;
    msg.data = j.dump();
    offer_id_pub_->publish(msg);
}

void WebRTCStreamer::on_offer_created(GstPromise* promise, gpointer user_data) {
    auto* ctx = static_cast<OfferContext*>(user_data);  // freed by offer_context_free with the promise
    auto* self = ctx->self;

    gst_promise_wait(promise);
    const GstStructure* reply = gst_promise_get_reply(promise);
    GstWebRTCSessionDescription* offer = nullptr;
    gst_structure_get(reply, "offer", GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &offer, nullptr);
    if (!offer) {
        RCLCPP_ERROR(self->get_logger(), "Failed to create offer");
        gst_promise_unref(promise);
        return;
    }

    // Drop the offer if the peer was torn down / replaced while it was being built (lock-free check).
    if (ctx->gen->load(std::memory_order_relaxed) != ctx->gen_value) {
        RCLCPP_INFO(self->get_logger(), "Discarding stale offer for '%s'", peer_label(ctx->client_id));
        gst_webrtc_session_description_free(offer);
        gst_promise_unref(promise);
        return;
    }

    // Safety net: never publish/apply an offer with no media. Applying the matching (empty) answer to a
    // webrtcbin that still holds transceivers is the _connect_input_stream abort. on-negotiation-needed
    // should prevent this, but a dropped offer just trips the connect timeout — an abort kills the robot.
    const SdpMediaCounts counts = count_sdp_media(offer->sdp);
    const bool missing_video = counts.video < ctx->expected_videos;
    const bool missing_audio = ctx->expected_audio && counts.audio == 0;
    if (missing_video || missing_audio) {
        RCLCPP_WARN(self->get_logger(), "Dropping incomplete offer for '%s' (video=%u/%u audio=%u%s)",
                    peer_label(ctx->client_id), counts.video, ctx->expected_videos, counts.audio,
                    ctx->expected_audio ? " required" : "");
        // Do not retry create-offer on this same webrtcbin; an immediate second offer can disrupt it. The
        // client offer watchdog will send a renegotiate START, which creates a fresh transport; until then
        // the connect timeout releases this peer. The offer-once latch (set when this offer was kicked off)
        // stays latched, so this webrtcbin never re-offers.
        gst_webrtc_session_description_free(offer);
        gst_promise_unref(promise);
        return;
    }

    g_signal_emit_by_name(ctx->webrtc, "set-local-description", offer, nullptr);  // fire-and-forget

    gchar* sdp_text = gst_sdp_message_as_text(offer->sdp);
    std::string sdp_str(sdp_text);
    g_free(sdp_text);
    self->publish_offer(ctx->client_id, sdp_str);
    RCLCPP_INFO(self->get_logger(), "Sent offer for '%s' (%zu bytes)", peer_label(ctx->client_id), sdp_str.size());

    gst_webrtc_session_description_free(offer);
    gst_promise_unref(promise);
}

void WebRTCStreamer::apply_answer(Peer* peer, const std::string& sdp) {
    if (sdp.empty() || sdp.size() > kMaxSdpBytes) {
        RCLCPP_WARN(this->get_logger(), "Ignoring SDP answer for '%s': invalid size %zu", peer_label(peer->client_id),
                    sdp.size());
        return;
    }
    GstSDPMessage* sdp_msg = nullptr;
    if (gst_sdp_message_new_from_text(sdp.c_str(), &sdp_msg) != GST_SDP_OK) {
        RCLCPP_WARN(this->get_logger(), "Ignoring SDP answer for '%s': parse failed", peer_label(peer->client_id));
        return;
    }
    const SdpMediaCounts counts = count_sdp_media(sdp_msg);
    const bool missing_video = counts.video < peer->videos.size();
    const bool missing_audio = peer->with_audio && counts.audio == 0;
    if (missing_video || missing_audio) {
        RCLCPP_WARN(this->get_logger(), "Ignoring incomplete SDP answer for '%s' (video=%u/%zu audio=%u%s)",
                    peer_label(peer->client_id), counts.video, peer->videos.size(), counts.audio,
                    peer->with_audio ? " required" : "");
        gst_sdp_message_free(sdp_msg);
        return;
    }
    GstWebRTCSessionDescription* answer = gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_ANSWER, sdp_msg);
    // Fire-and-forget: pass a NULL promise (we don't observe the result), same as set-local-description in
    // on_offer_created. Passing a gst_promise_new() here would leak it — it's never waited on or unref'd.
    g_signal_emit_by_name(peer->webrtc, "set-remote-description", answer, nullptr);
    gst_webrtc_session_description_free(answer);
    RCLCPP_INFO(this->get_logger(), "Answer set for '%s'", peer_label(peer->client_id));
}

void WebRTCStreamer::apply_ice(Peer* peer, const std::string& candidate, int mline) {
    g_signal_emit_by_name(peer->webrtc, "add-ice-candidate", mline, candidate.c_str());
}

void WebRTCStreamer::deliver_answer(const std::string& client_id, const std::string& sdp) {
    if (!is_safe_client_id(client_id)) {
        RCLCPP_WARN(this->get_logger(), "Ignoring answer with invalid client_id");
        return;
    }
    if (sdp.empty() || sdp.size() > kMaxSdpBytes) {
        RCLCPP_WARN(this->get_logger(), "Ignoring answer for '%s': invalid SDP size %zu", client_id.c_str(),
                    sdp.size());
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        RCLCPP_WARN(this->get_logger(), "answer for unknown peer '%s'", client_id.c_str());
        return;
    }
    apply_answer(it->second.get(), sdp);
}

void WebRTCStreamer::deliver_ice(const std::string& client_id, const std::string& candidate, int mline) {
    if (!is_safe_client_id(client_id)) {
        RCLCPP_WARN(this->get_logger(), "Ignoring ICE with invalid client_id");
        return;
    }
    if (candidate.empty()) {
        return;  // end-of-candidates marker; libnice does not need it here
    }
    if (candidate.size() > kMaxIceCandidateBytes || candidate.rfind("candidate:", 0) != 0) {
        RCLCPP_WARN(this->get_logger(), "Ignoring ICE for '%s': invalid candidate size/shape", client_id.c_str());
        return;
    }
    if (mline < 0) {
        RCLCPP_WARN(this->get_logger(), "Ignoring ICE for '%s': negative m-line %d", client_id.c_str(), mline);
        return;
    }
    std::lock_guard<std::mutex> lock(peers_mutex_);
    auto it = peers_.find(client_id);
    if (it != peers_.end()) {
        Peer* peer = it->second.get();
        const int max_mline = static_cast<int>(peer->videos.size()) + (peer->with_audio ? 1 : 0);
        if (mline >= max_mline) {
            RCLCPP_WARN(this->get_logger(), "Ignoring ICE for '%s': m-line %d outside negotiated range [0,%d)",
                        client_id.c_str(), mline, max_mline - 1);
            return;
        }
        const std::string addr = candidate_address(candidate);
        if (is_mdns_address(addr)) {
            if (peer->have_real_remote_ice) {
                RCLCPP_INFO(this->get_logger(), "Dropping late remote mDNS ICE candidate '%s'; real ICE exists",
                            addr.c_str());
                return;
            }
            if (peer->pending_mdns_ice.size() >= kMaxPendingMdnsIce) {
                RCLCPP_WARN(this->get_logger(), "Dropping remote mDNS ICE candidate for '%s': pending queue full",
                            client_id.c_str());
                return;
            }
            peer->pending_mdns_ice.emplace_back(mline, candidate);
            if (peer->first_mdns_ice_ns == 0) {
                peer->first_mdns_ice_ns = std::chrono::steady_clock::now().time_since_epoch().count();
            }
            RCLCPP_INFO(this->get_logger(), "Deferring remote mDNS ICE candidate '%s' for local-STUN srflx",
                        addr.c_str());
            return;
        }
        if (!is_mdns_address(addr)) {
            peer->have_real_remote_ice = true;
            if (!peer->pending_mdns_ice.empty()) {
                RCLCPP_INFO(this->get_logger(),
                            "Using real remote ICE candidate '%s'; dropping %zu deferred mDNS candidate(s)",
                            addr.empty() ? "(unknown)" : addr.c_str(), peer->pending_mdns_ice.size());
                peer->pending_mdns_ice.clear();
            }
        }
        apply_ice(peer, candidate, mline);
    }
}

void WebRTCStreamer::on_answer_id(const std_msgs::msg::String::SharedPtr msg) {
    nlohmann::json j;
    if (!parse_json_object(msg->data, kMaxSignalMessageBytes, &j) || !j.contains("client_id") ||
        !j["client_id"].is_string() || !j.contains("sdp") || !j["sdp"].is_string()) {
        RCLCPP_WARN(this->get_logger(), "Ignoring malformed answer_id (%zu bytes)", msg->data.size());
        return;
    }
    deliver_answer(j["client_id"].get<std::string>(), j["sdp"].get<std::string>());
}

void WebRTCStreamer::on_ice_in_id(const std_msgs::msg::String::SharedPtr msg) {
    nlohmann::json j;
    if (!parse_json_object(msg->data, kMaxSignalMessageBytes, &j) || !j.contains("client_id") ||
        !j["client_id"].is_string() || !j.contains("candidate") || !j["candidate"].is_string() ||
        !j.contains("sdpMLineIndex") || !j["sdpMLineIndex"].is_number_integer()) {
        RCLCPP_WARN(this->get_logger(), "Ignoring malformed ice_in_id (%zu bytes)", msg->data.size());
        return;
    }
    int mline = 0;
    try {
        if (j["sdpMLineIndex"].is_number_unsigned()) {
            const auto u = j["sdpMLineIndex"].get<uint64_t>();
            if (u > static_cast<uint64_t>(std::numeric_limits<int>::max())) {
                RCLCPP_WARN(this->get_logger(), "Ignoring ice_in_id with out-of-range m-line");
                return;
            }
            mline = static_cast<int>(u);
        } else {
            const auto i = j["sdpMLineIndex"].get<int64_t>();
            if (i < 0 || i > std::numeric_limits<int>::max()) {
                RCLCPP_WARN(this->get_logger(), "Ignoring ice_in_id with out-of-range m-line");
                return;
            }
            mline = static_cast<int>(i);
        }
    } catch (const nlohmann::json::exception&) {
        RCLCPP_WARN(this->get_logger(), "Ignoring ice_in_id with out-of-range m-line");
        return;
    }
    deliver_ice(j["client_id"].get<std::string>(), j["candidate"].get<std::string>(), mline);
}

void WebRTCStreamer::on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    // Send all of our own candidates (IPv4 LAN, tailscale, IPv6); the browser picks what reaches it.
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));
    std::string client_id = cid ? cid : "";

    if (!is_safe_client_id(client_id)) {
        RCLCPP_WARN(self->get_logger(), "Dropping outgoing ICE candidate without valid client_id");
        return;
    }
    nlohmann::json json;
    json["client_id"] = client_id;
    json["candidate"] = std::string(candidate ? candidate : "");
    json["sdpMLineIndex"] = mline;

    auto msg = std_msgs::msg::String();
    msg.data = json.dump();
    self->ice_out_id_pub_->publish(msg);
}

void WebRTCStreamer::on_connection_state_changed(GstElement* webrtc, GParamSpec*, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    GstWebRTCPeerConnectionState state;
    g_object_get(webrtc, "connection-state", &state, nullptr);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));

    // On connect, force a keyframe on every encoder so this peer (which may be joining a stream that's
    // already running for others) gets a decodable IDR immediately. Teardown is handled by the health
    // poll on the executor thread (set-state from here would deadlock the pipeline).
    //
    // This callback runs on webrtcbin's PC thread and must stay LOCK-FREE: ~Peer holds peers_mutex_
    // while set_state(NULL) joins this very thread, so taking the mutex here is an ABBA deadlock that
    // wedges the whole node. media_ready is a shared atomic tagged on the element for exactly this
    // reason; it outlives the Peer, so a late notify after teardown is harmless.
    if (auto* ready =
            static_cast<std::shared_ptr<std::atomic<bool>>*>(g_object_get_data(G_OBJECT(webrtc), "mars_media_ready"))) {
        (*ready)->store(state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED, std::memory_order_relaxed);
    }
    if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
        for (auto& cam : self->cameras_)
            self->force_keyframe(cam->name);
    }
    RCLCPP_INFO(self->get_logger(), "Peer '%s' connection state: %s", (cid && *cid) ? cid : "(default)",
                conn_state_name(state));
}

}  // namespace mars_cam
