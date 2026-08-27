// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <gst/rtp/rtp.h>
#include <gst/app/app.h>

#include <sstream>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <chrono>
#include <memory>

namespace mars_cam {

namespace {
// MarsPlayoutDelayExt GObject impl (constants/URI + make_playout_delay_ext decl live in webrtc_internal.hpp).
struct MarsPlayoutDelayExt {
    GstRTPHeaderExtension parent;
    guint min_delay_ms;
    guint max_delay_ms;
};
struct MarsPlayoutDelayExtClass {
    GstRTPHeaderExtensionClass parent_class;
};

G_DEFINE_TYPE(MarsPlayoutDelayExt, mars_playout_delay_ext, GST_TYPE_RTP_HEADER_EXTENSION)

GstRTPHeaderExtensionFlags mars_playout_delay_supported_flags(GstRTPHeaderExtension*) {
    return static_cast<GstRTPHeaderExtensionFlags>(GST_RTP_HEADER_EXTENSION_ONE_BYTE |
                                                   GST_RTP_HEADER_EXTENSION_TWO_BYTE);
}

gsize mars_playout_delay_max_size(GstRTPHeaderExtension*, const GstBuffer*) {
    return 3;
}

gssize mars_playout_delay_write(GstRTPHeaderExtension* ext, const GstBuffer*, GstRTPHeaderExtensionFlags, GstBuffer*,
                                guint8* data, gsize size) {
    if (size < 3) {
        return -1;
    }
    auto* self = reinterpret_cast<MarsPlayoutDelayExt*>(ext);
    const guint min_units = (self->min_delay_ms / 10) & 0xFFF;  // 12-bit, 10 ms units
    const guint max_units = (self->max_delay_ms / 10) & 0xFFF;
    data[0] = static_cast<guint8>(min_units >> 4);
    data[1] = static_cast<guint8>(((min_units & 0xF) << 4) | ((max_units >> 8) & 0xF));
    data[2] = static_cast<guint8>(max_units & 0xFF);
    return 3;
}

void mars_playout_delay_ext_class_init(MarsPlayoutDelayExtClass* klass) {
    GstRTPHeaderExtensionClass* ext_class = GST_RTP_HEADER_EXTENSION_CLASS(klass);
    ext_class->get_supported_flags = mars_playout_delay_supported_flags;
    ext_class->get_max_size = mars_playout_delay_max_size;
    ext_class->write = mars_playout_delay_write;
    gst_rtp_header_extension_class_set_uri(ext_class, MARS_PLAYOUT_DELAY_URI);
    gst_element_class_set_metadata(GST_ELEMENT_CLASS(klass), "Playout delay RTP header extension",
                                   GST_RTP_HDREXT_ELEMENT_CLASS, "WebRTC playout-delay header extension", "mars_cam");
}

void mars_playout_delay_ext_init(MarsPlayoutDelayExt* self) {
    self->min_delay_ms = 0;
    self->max_delay_ms = 0;
}
}  // namespace

GstRTPHeaderExtension* make_playout_delay_ext(guint ext_id, guint min_ms, guint max_ms) {
    auto* self = static_cast<MarsPlayoutDelayExt*>(g_object_new(mars_playout_delay_ext_get_type(), nullptr));
    self->min_delay_ms = min_ms;
    self->max_delay_ms = max_ms;
    gst_rtp_header_extension_set_id(GST_RTP_HEADER_EXTENSION(self), ext_id);
    return GST_RTP_HEADER_EXTENSION(self);
}

WebRTCStreamer::WebRTCStreamer(const rclcpp::NodeOptions& options)
    : Node("webrtc_streamer", options), camera_qos_(rclcpp::QoS(1).best_effort()) {
    gst_init(nullptr, nullptr);

    this->declare_parameter("use_compressed_images", false);
    this->declare_parameter("enable_audio", true);
    this->declare_parameter("audio_source_element", "alsasrc");
    this->declare_parameter("audio_capture_device", "");
    this->declare_parameter("playout_min_delay_ms", 0);
    this->declare_parameter("playout_max_delay_ms", 40);
    // Video loss repair. ULPFEC repairs without a round trip at ~fec% bitrate overhead (0 disables);
    // NACK retransmits only pay off when the browser's jitter buffer (playout_max_delay_ms) can wait
    // roughly one RTT for the resend, so remote viewers should raise that param too.
    this->declare_parameter("video_nack", true);
    this->declare_parameter("video_fec_percentage", 25);
    this->declare_parameter("enable_local_stun", true);
    this->declare_parameter("local_stun_port", 3478);
    // RTCP-inactivity release only fires on a peer webrtcbin still reports CONNECTED — it's a backstop for
    // a peer that silently went away. Keep it well above the RTCP receiver-report interval (which can be
    // ~5 s for low-bitrate video) so a single late report doesn't tear down a live connection; genuinely
    // failed peers are caught faster by the connection-state (DISCONNECTED/FAILED) path.
    this->declare_parameter("rtcp_inactivity_timeout_s", kDefaultRtcpInactivityTimeoutS);

    use_compressed_images_ = this->get_parameter("use_compressed_images").as_bool();
    enable_audio_ = this->get_parameter("enable_audio").as_bool();
    audio_source_element_ = this->get_parameter("audio_source_element").as_string();
    audio_capture_device_ = this->get_parameter("audio_capture_device").as_string();
    playout_min_delay_ms_ = static_cast<guint>(this->get_parameter("playout_min_delay_ms").as_int());
    playout_max_delay_ms_ = static_cast<guint>(this->get_parameter("playout_max_delay_ms").as_int());
    video_nack_ = this->get_parameter("video_nack").as_bool();
    video_fec_percentage_ =
        static_cast<guint>(std::clamp(this->get_parameter("video_fec_percentage").as_int(), int64_t{0}, int64_t{100}));
    enable_local_stun_ = this->get_parameter("enable_local_stun").as_bool();
    local_stun_port_ = static_cast<int>(this->get_parameter("local_stun_port").as_int());
    rtcp_inactivity_timeout_s_ = this->get_parameter("rtcp_inactivity_timeout_s").as_double();
    rtcp_timeout_cb_ = this->add_on_set_parameters_callback([this](const std::vector<rclcpp::Parameter>& params) {
        rcl_interfaces::msg::SetParametersResult result;
        result.successful = true;
        for (const auto& p : params) {
            if (p.get_name() != "rtcp_inactivity_timeout_s") {
                continue;
            }
            const double v = p.as_double();
            if (v <= 0.0) {
                result.successful = false;
                result.reason = "rtcp_inactivity_timeout_s must be > 0";
            } else {
                rtcp_inactivity_timeout_s_ = v;
                RCLCPP_INFO(this->get_logger(), "rtcp_inactivity_timeout_s set to %.1f s", v);
            }
        }
        return result;
    });

    // Publishers/subscribers use client_id envelopes so multiple peers can negotiate independently on
    // shared topics. The old bare raw-SDP topics are intentionally not exposed.
    offer_id_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/offer_id", 10);
    ice_out_id_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/ice_out_id", 10);
    active_streams_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/active_streams", 10);

    start_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/start", 10, std::bind(&WebRTCStreamer::on_start, this, std::placeholders::_1));
    answer_id_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/answer_id", 10, std::bind(&WebRTCStreamer::on_answer_id, this, std::placeholders::_1));
    ice_in_id_sub_ = this->create_subscription<std_msgs::msg::String>(
        "/webrtc/ice_in_id", 10, std::bind(&WebRTCStreamer::on_ice_in_id, this, std::placeholders::_1));

    // The encoders run for the node's lifetime; only the per-peer transport churns. Configure the camera
    // set, build them now — the camera callbacks gate the actual CPU, and the topics are subscribed lazily
    // (reconcile_subscriptions on the first peer), so an idle node receives no camera frames.
    configure_cameras();
    if (!build_encode_pipeline()) {
        RCLCPP_FATAL(this->get_logger(), "Failed to build the persistent encode pipeline");
        throw std::runtime_error("encode pipeline build failed");
    }
    // Shared mic pipeline (encoded once, fanned out, gated NULL/PLAYING for privacy). Optional: if it
    // can't be built (no mic), carry on video-only.
    if (enable_audio_ && !build_audio_pipeline()) {
        RCLCPP_WARN(this->get_logger(), "Audio pipeline build failed; continuing video-only");
        enable_audio_ = false;
    }
    start_local_stun_server();

    health_timer_ =
        this->create_wall_timer(std::chrono::milliseconds(200), std::bind(&WebRTCStreamer::poll_pipeline_health, this));
    prev_status_time_ = std::chrono::steady_clock::now();
    status_timer_ = this->create_wall_timer(std::chrono::seconds(2), std::bind(&WebRTCStreamer::publish_status, this));
    adapt_timer_ =
        this->create_wall_timer(std::chrono::seconds(1), std::bind(&WebRTCStreamer::poll_network_adaptation, this));

    RCLCPP_INFO(this->get_logger(), "WebRTC Streamer ready (%zu cameras, source: %s, compressed: %s)", cameras_.size(),
                current_source_.c_str(), use_compressed_images_ ? "true" : "false");
    RCLCPP_INFO(this->get_logger(), "  Mic audio: %s", enable_audio_ ? "enabled (opt-in per peer)" : "disabled");
    RCLCPP_INFO(this->get_logger(), "  Local STUN: %s", enable_local_stun_ ? "enabled" : "disabled");
    RCLCPP_INFO(this->get_logger(), "  Video loss repair: nack=%s, fec=%u%%", video_nack_ ? "on" : "off",
                video_fec_percentage_);
    RCLCPP_INFO(this->get_logger(), "  RTCP-inactivity teardown: %.1f s", rtcp_inactivity_timeout_s_);
}

void WebRTCStreamer::configure_cameras() {
    // `cameras` lists the camera names (m-line order). Each gets per-camera params:
    //   live_<name>_camera_topic, replay_<name>_camera_topic, <name>_fps, <name>_width, <name>_height
    // The built-in `main`/`arm` keep their existing topic/fps defaults (so existing launches are
    // unchanged); any other name must supply its own topics. PT + SSRC are assigned by index.
    static const std::map<std::string, std::tuple<std::string, std::string, int>> kDefaults = {
        {"main", {"/mars/main_camera/left/image_raw", "/brain/recorder/replay/main_camera/left/image_raw", 30}},
        {"arm", {"/mars/arm/image_raw", "/brain/recorder/replay/arm_camera/image_raw", 15}},
    };
    const auto names = this->declare_parameter<std::vector<std::string>>("cameras", {"main", "arm"});
    for (const auto& name : names) {
        if (cameras_.size() >= kMaxCameras) {
            RCLCPP_ERROR(this->get_logger(), "Camera '%s' dropped: the RTP payload-type space fits %zu cameras",
                         name.c_str(), kMaxCameras);
            continue;
        }
        std::string def_live, def_replay;
        int def_fps = 30;
        if (auto it = kDefaults.find(name); it != kDefaults.end()) {
            std::tie(def_live, def_replay, def_fps) = it->second;
        }
        auto cam = std::make_unique<CameraEncoder>();
        cam->name = name;
        cam->live_topic = this->declare_parameter<std::string>("live_" + name + "_camera_topic", def_live);
        cam->replay_topic = this->declare_parameter<std::string>("replay_" + name + "_camera_topic", def_replay);
        cam->fps = static_cast<int>(this->declare_parameter<int>(name + "_fps", def_fps));
        cam->width = static_cast<int>(this->declare_parameter<int>(name + "_width", 640));
        cam->height = static_cast<int>(this->declare_parameter<int>(name + "_height", 480));
        cam->bitrate_kbps = static_cast<int>(this->declare_parameter<int>(name + "_bitrate_kbps", 2000));
        cam->pt = cam_pt_for_index(cameras_.size());
        cam->ssrc = cam_ssrc_for_index(cameras_.size());
        cam->owner = this;
        if (cam->live_topic.empty()) {
            RCLCPP_WARN(this->get_logger(), "Camera '%s' has no live topic configured; skipping it", name.c_str());
            continue;
        }
        RCLCPP_INFO(this->get_logger(), "  Camera[%zu] '%s': pt=%d ssrc=0x%08X %dx%d@%dfps %dkbps live=%s",
                    cameras_.size(), name.c_str(), cam->pt, cam->ssrc, cam->width, cam->height, cam->fps,
                    cam->bitrate_kbps, cam->live_topic.c_str());
        cameras_.push_back(std::move(cam));
    }
    if (cameras_.empty()) {
        RCLCPP_FATAL(this->get_logger(), "No cameras configured (the `cameras` parameter is empty)");
        throw std::runtime_error("no cameras configured");
    }
}

CameraEncoder* WebRTCStreamer::find_camera(const std::string& name) {
    for (auto& cam : cameras_) {
        if (cam->name == name) {
            return cam.get();
        }
    }
    return nullptr;
}

WebRTCStreamer::~WebRTCStreamer() {
    stop_local_stun_server();
    health_timer_.reset();
    status_timer_.reset();
    adapt_timer_.reset();
    destroy_subscriptions();
    {
        std::lock_guard<std::mutex> lock(peers_mutex_);
        std::vector<std::string> ids;
        for (auto& kv : peers_) {
            ids.push_back(kv.first);
        }
        for (const auto& id : ids) {
            destroy_peer(id);
        }
    }
    // Tear down the persistent encode pipeline.
    for (auto& cam : cameras_) {
        if (cam->pool) {
            gst_buffer_pool_set_active(cam->pool, FALSE);
            gst_object_unref(cam->pool);
        }
        if (cam->appsrc)
            gst_object_unref(cam->appsrc);
        if (cam->sink)
            gst_object_unref(cam->sink);
    }
    if (encode_pipeline_) {
        gst_element_set_state(encode_pipeline_, GST_STATE_NULL);
        gst_object_unref(encode_pipeline_);
    }
    if (audio_sink_)
        gst_object_unref(audio_sink_);
    if (audio_pipeline_) {
        gst_element_set_state(audio_pipeline_, GST_STATE_NULL);
        gst_object_unref(audio_pipeline_);
    }
}

// =============================================================================
// RTCP-inactivity watchdog + health poll
// =============================================================================

GstPadProbeReturn WebRTCStreamer::on_rtcp_buffer(GstPad*, GstPadProbeInfo*, gpointer user_data) {
    auto* peer = static_cast<Peer*>(user_data);
    peer->last_rtcp_ns.store(std::chrono::steady_clock::now().time_since_epoch().count(), std::memory_order_relaxed);
    return GST_PAD_PROBE_OK;
}

bool WebRTCStreamer::install_rtcp_probe_for(Peer* peer) {
    GstElement* rtpbin = gst_bin_get_by_name(GST_BIN(peer->webrtc), "rtpbin");
    if (!rtpbin) {
        return false;
    }
    bool installed = false;
    GstIterator* it = gst_element_iterate_sink_pads(rtpbin);
    GValue item = G_VALUE_INIT;
    bool done = false;
    while (!done) {
        switch (gst_iterator_next(it, &item)) {
            case GST_ITERATOR_OK: {
                GstPad* pad = GST_PAD(g_value_get_object(&item));
                gchar* name = gst_pad_get_name(pad);
                if (name && g_str_has_prefix(name, "recv_rtcp_sink")) {
                    gst_pad_add_probe(
                        pad, static_cast<GstPadProbeType>(GST_PAD_PROBE_TYPE_BUFFER | GST_PAD_PROBE_TYPE_BUFFER_LIST),
                        on_rtcp_buffer, peer, nullptr);
                    installed = true;
                }
                g_free(name);
                g_value_reset(&item);
                break;
            }
            case GST_ITERATOR_RESYNC:
                gst_iterator_resync(it);
                break;
            case GST_ITERATOR_ERROR:
            case GST_ITERATOR_DONE:
                done = true;
                break;
        }
    }
    g_value_unset(&item);
    gst_iterator_free(it);
    gst_object_unref(rtpbin);
    return installed;
}

// One get-stats reply: keep the worst remote-inbound-rtp (the RTCP receiver-report echo) loss/RTT
// seen this round. Fires on a webrtcbin thread; only atomics are touched (node outlives promises —
// same lifetime contract as OfferContext).
void WebRTCStreamer::on_peer_stats(GstPromise* promise, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const GstStructure* reply = gst_promise_get_reply(promise);
    if (!reply) {
        gst_promise_unref(promise);
        return;
    }
    gst_structure_foreach(
        reply,
        [](GQuark, const GValue* value, gpointer data) -> gboolean {
            auto* self = static_cast<WebRTCStreamer*>(data);
            if (!GST_VALUE_HOLDS_STRUCTURE(value)) {
                return TRUE;
            }
            const GstStructure* s = gst_value_get_structure(value);
            GstWebRTCStatsType type;
            if (!gst_structure_get(s, "type", GST_TYPE_WEBRTC_STATS_TYPE, &type, nullptr) ||
                type != GST_WEBRTC_STATS_REMOTE_INBOUND_RTP) {
                return TRUE;
            }
            // fraction-lost is the RR's loss as a double in [0,1); round-trip-time is seconds (double).
            // (rb-fractionlost exists only on webrtcbin's INTERNAL input stats, never in this reply —
            // reading it here left the controller loss-blind, adapting on RTT alone.)
            if (gdouble fl = 0.0; gst_structure_get_double(s, "fraction-lost", &fl)) {
                const int promille = static_cast<int>(fl * 1000.0);
                if (promille > self->rtcp_loss_promille_.load(std::memory_order_relaxed)) {
                    self->rtcp_loss_promille_.store(promille, std::memory_order_relaxed);
                }
            }
            if (gdouble rtt_s = 0.0; gst_structure_get_double(s, "round-trip-time", &rtt_s)) {
                const int ms = static_cast<int>(rtt_s * 1000.0);
                if (ms > self->rtcp_rtt_ms_.load(std::memory_order_relaxed)) {
                    self->rtcp_rtt_ms_.store(ms, std::memory_order_relaxed);
                }
            }
            return TRUE;
        },
        self);
    gst_promise_unref(promise);
}

void WebRTCStreamer::apply_adaptation(AdaptRung rung, int loss_promille, int rtt_ms) {
    adapt_rung_ = rung;
    degraded_ = rung == AdaptRung::kDegraded;
    // Shedding is the point: the old DEGRADED (60% bitrate x 100% FEC) offered 1800 kbps against
    // GOOD's 1875 — a 4% cut with double the packets, feeding the congestion it answered.
    const int tenths = rung == AdaptRung::kDegraded ? 4 : rung == AdaptRung::kRecovering ? 7 : 10;
    for (auto& cam : cameras_) {
        GstElement* enc = gst_bin_get_by_name(GST_BIN(encode_pipeline_), ("enc_" + cam->name).c_str());
        if (!enc) {
            continue;
        }
        const int bps = cam->bitrate_kbps * 1000 * tenths / 10;
        g_object_set(enc, "target-bitrate", bps, nullptr);
        gst_object_unref(enc);
    }
    // FEC is graduated down the ladder: full at the bottom (see degraded_fec_pct — residual burst
    // loss on the remote leg is what FEC exists for, and 100% of a 40% bitrate is still cheap),
    // 2x base while RECOVERING probes upward, base at GOOD.
    if (video_fec_percentage_ > 0) {
        const guint pct = rung == AdaptRung::kDegraded     ? degraded_fec_pct()
                          : rung == AdaptRung::kRecovering ? std::min(100u, video_fec_percentage_ * 2)
                                                           : video_fec_percentage_;
        std::lock_guard<std::mutex> lock(peers_mutex_);
        for (auto& kv : peers_) {
            for (size_t i = 0; i < kv.second->videos.size(); ++i) {
                GstWebRTCRTPTransceiver* trans = nullptr;
                g_signal_emit_by_name(kv.second->webrtc, "get-transceiver", static_cast<gint>(i), &trans);
                if (trans) {
                    g_object_set(trans, "fec-percentage", pct, nullptr);
                    g_object_unref(trans);
                }
            }
        }
    }
    const char* state = rung == AdaptRung::kDegraded     ? "DEGRADED: 40% bitrate + full FEC"
                        : rung == AdaptRung::kRecovering ? "RECOVERING: 70% bitrate + 2x FEC"
                                                         : "GOOD: full bitrate + base FEC";
    if (loss_promille < 0 && rtt_ms < 0) {
        RCLCPP_INFO(this->get_logger(), "Network adaptation -> %s (no viewer reports)", state);
    } else if (loss_promille < 0) {
        RCLCPP_INFO(this->get_logger(), "Network adaptation -> %s (viewer loss n/a, rtt %d ms)", state, rtt_ms);
    } else {
        RCLCPP_INFO(this->get_logger(), "Network adaptation -> %s (viewer loss %.1f%%, rtt %d ms)", state,
                    loss_promille / 10.0, rtt_ms);
    }
}

void WebRTCStreamer::poll_network_adaptation() {
    const int loss = rtcp_loss_promille_.exchange(-1, std::memory_order_relaxed);
    const int rtt = rtcp_rtt_ms_.exchange(-1, std::memory_order_relaxed);

    // Kick off this round of per-peer stats (answers land before the next tick).
    bool any_peers = false;
    {
        std::lock_guard<std::mutex> lock(peers_mutex_);
        any_peers = !peers_.empty();
        for (auto& kv : peers_) {
            if (!kv.second->media_ready->load(std::memory_order_relaxed)) {
                continue;
            }
            GstPromise* promise = gst_promise_new_with_change_func(on_peer_stats, this, nullptr);
            g_signal_emit_by_name(kv.second->webrtc, "get-stats", nullptr, promise);
        }
    }

    // Last viewer gone: back to GOOD now, so the next peer (LAN included) isn't served a rung
    // frozen from a link that no longer exists.
    if (!any_peers) {
        adapt_bad_ticks_ = 0;
        adapt_good_ticks_ = 0;
        if (adapt_rung_ != AdaptRung::kGood) {
            apply_adaptation(AdaptRung::kGood, loss, rtt);
        }
        return;
    }

    // Drop to the bottom rung fast (2 s of loss), climb ONE rung per 15 clean s — a one-step
    // DEGRADED→GOOD exit doubles the offered load at once and re-congests the path (measured:
    // 34 flips/hour on a tunneled link). Loss-only: RTT is a property of the path, not damage;
    // a tick without a loss measurement leaves the counters unchanged (RTT alone can't prove
    // the link clean). Recovery keyframes stay purely PLI-driven on every rung — a forced IDR
    // is the most expensive burst a congested link can be handed.
    const bool report = loss >= 0;
    const bool bad = loss >= 30;
    if (report) {
        adapt_bad_ticks_ = bad ? adapt_bad_ticks_ + 1 : 0;
        adapt_good_ticks_ = bad ? 0 : adapt_good_ticks_ + 1;
    }
    if (adapt_bad_ticks_ >= 2 && adapt_rung_ != AdaptRung::kDegraded) {
        apply_adaptation(AdaptRung::kDegraded, loss, rtt);
    } else if (adapt_good_ticks_ >= 15 && adapt_rung_ != AdaptRung::kGood) {
        adapt_good_ticks_ = 0;  // the next rung needs its own clean window
        apply_adaptation(adapt_rung_ == AdaptRung::kDegraded ? AdaptRung::kRecovering : AdaptRung::kGood, loss, rtt);
    }
}

void WebRTCStreamer::poll_pipeline_health() {
    // expected_teardown: a peer's DTLS error on disconnect is routine (the peer
    // is released below), unlike a fault on the shared encode/audio pipelines.
    auto drain_bus = [this](GstElement* pipeline, const char* label, bool expected_teardown) {
        bool saw_error = false;
        if (!pipeline) {
            return saw_error;
        }
        GstBus* bus = gst_element_get_bus(pipeline);
        while (GstMessage* m = gst_bus_pop(bus)) {
            switch (GST_MESSAGE_TYPE(m)) {
                case GST_MESSAGE_ERROR: {
                    saw_error = true;
                    GError* err = nullptr;
                    gchar* debug = nullptr;
                    gst_message_parse_error(m, &err, &debug);
                    if (expected_teardown) {
                        RCLCPP_WARN(this->get_logger(), "GStreamer %s error from %s: %s", label,
                                    GST_OBJECT_NAME(m->src), err ? err->message : "unknown");
                    } else {
                        RCLCPP_ERROR(this->get_logger(), "GStreamer %s error from %s: %s%s%s", label,
                                     GST_OBJECT_NAME(m->src), err ? err->message : "unknown", debug ? " debug=" : "",
                                     debug ? debug : "");
                    }
                    g_clear_error(&err);
                    g_free(debug);
                    break;
                }
                case GST_MESSAGE_WARNING: {
                    GError* err = nullptr;
                    gchar* debug = nullptr;
                    gst_message_parse_warning(m, &err, &debug);
                    RCLCPP_WARN(this->get_logger(), "GStreamer %s warning from %s: %s%s%s", label,
                                GST_OBJECT_NAME(m->src), err ? err->message : "unknown", debug ? " debug=" : "",
                                debug ? debug : "");
                    g_clear_error(&err);
                    g_free(debug);
                    break;
                }
                default:
                    break;
            }
            gst_message_unref(m);
        }
        gst_object_unref(bus);
        return saw_error;
    };

    drain_bus(encode_pipeline_, "encode", /*expected_teardown=*/false);
    drain_bus(audio_pipeline_, "audio", /*expected_teardown=*/false);

    std::unique_lock<std::mutex> lock(peers_mutex_);
    if (peers_.empty()) {
        lock.unlock();
        reconcile_audio();  // no peers -> close the mic (safe: done without peers_mutex_ held)
        return;
    }
    const int64_t now_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    std::vector<std::string> dead;

    for (auto& kv : peers_) {
        Peer* p = kv.second.get();
        // Drain this peer's bus so runtime errors are logged.
        if (drain_bus(p->pipeline, "transport", /*expected_teardown=*/true)) {
            RCLCPP_INFO(this->get_logger(), "Peer '%s' transport bus error; releasing",
                        kv.first.empty() ? "(default)" : kv.first.c_str());
            dead.push_back(kv.first);
            continue;
        }

        const GstWebRTCPeerConnectionState state = peer_connection_state(p->webrtc);
        if (!p->have_real_remote_ice && !p->pending_mdns_ice.empty() && p->first_mdns_ice_ns != 0 &&
            (now_ns - p->first_mdns_ice_ns) / 1e9 > kMdnsIceFallbackDelayS) {
            RCLCPP_WARN(
                this->get_logger(),
                "Peer '%s' got no real remote ICE candidate after %.1f s; flushing %zu mDNS fallback candidate(s)",
                kv.first.empty() ? "(default)" : kv.first.c_str(), kMdnsIceFallbackDelayS, p->pending_mdns_ice.size());
            const auto pending = std::move(p->pending_mdns_ice);
            p->pending_mdns_ice.clear();
            for (const auto& ice : pending) {
                apply_ice(p, ice.second, ice.first);
            }
        }
        const bool closed = state == GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED;
        const bool down =
            state == GST_WEBRTC_PEER_CONNECTION_STATE_FAILED || state == GST_WEBRTC_PEER_CONNECTION_STATE_DISCONNECTED;
        p->terminal_polls = down ? p->terminal_polls + 1 : 0;
        if (closed || p->terminal_polls >= kTeardownGracePolls) {
            RCLCPP_INFO(this->get_logger(), "Peer '%s' %s; releasing",
                        kv.first.empty() ? "(default)" : kv.first.c_str(),
                        closed ? "closed" : "down past grace window");
            dead.push_back(kv.first);
            continue;
        }

        // Release a peer that never finished connecting (failed ICE / abandoned handshake) so it can't
        // leak its transport and keep the encoder pinned on.
        if (!p->ever_connected && state != GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
            if ((now_ns - p->created_ns) / 1e9 > kConnectTimeoutS) {
                RCLCPP_WARN(this->get_logger(),
                            "Peer '%s' NO USABLE NETWORK PATH: ICE found no working candidate pair within "
                            "%.0f s. The robot could not reach this client on any route — its host candidates "
                            "are likely mDNS-obfuscated/unreachable and srflx (NAT hairpin) failed. Releasing.",
                            kv.first.empty() ? "(default)" : kv.first.c_str(), kConnectTimeoutS);
                dead.push_back(kv.first);
                continue;
            }
        }

        if (state == GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED) {
            p->ever_connected = true;
            if (!p->rtcp_probe_installed) {
                p->rtcp_probe_installed = install_rtcp_probe_for(p);
                if (p->rtcp_probe_installed) {
                    p->last_rtcp_ns.store(now_ns, std::memory_order_relaxed);  // arm from connect time
                }
            }
            const int64_t last = p->last_rtcp_ns.load(std::memory_order_relaxed);
            if (last != 0 && p->rtcp_probe_installed) {
                const double idle_s = (now_ns - last) / 1e9;
                if (idle_s > rtcp_inactivity_timeout_s_) {
                    RCLCPP_INFO(this->get_logger(), "Peer '%s' RTCP idle %.1f s (> %.1f s); releasing",
                                kv.first.empty() ? "(default)" : kv.first.c_str(), idle_s, rtcp_inactivity_timeout_s_);
                    dead.push_back(kv.first);
                }
            }
        }
    }

    for (const auto& id : dead) {
        destroy_peer(id);
    }
    lock.unlock();
    reconcile_audio();  // open/close the mic to match want_audio_ — OUTSIDE the lock (close joins a thread)
}

// =============================================================================
// Status
// =============================================================================

void WebRTCStreamer::publish_status() {
    const auto now = std::chrono::steady_clock::now();
    double dt = std::chrono::duration<double>(now - prev_status_time_).count();
    if (dt <= 1e-3)
        dt = 1e-3;
    prev_status_time_ = now;

    const bool replay = current_source_ == "replay";
    // Sample each camera's node-wide encode fps (each camera is encoded once; shared by all its viewers).
    struct CameraStatus {
        double input_fps = 0.0;
        double rtp_packet_fps = 0.0;
        std::string topic;
        uint64_t input_push_errors = 0;
        std::string input_flow;
    };
    std::map<std::string, CameraStatus> cam_info;
    for (auto& cam : cameras_) {
        const uint64_t in = cam->input_frames.load(std::memory_order_relaxed);
        const uint64_t encoded = cam->encoded_frames.load(std::memory_order_relaxed);
        CameraStatus status;
        status.input_fps = (in - cam->prev_input_frames) / dt;
        status.rtp_packet_fps = (encoded - cam->prev_encoded_frames) / dt;
        status.topic = replay ? cam->replay_topic : cam->live_topic;
        status.input_push_errors = cam->input_push_errors.load(std::memory_order_relaxed);
        status.input_flow =
            gst_flow_get_name(static_cast<GstFlowReturn>(cam->input_flow_code.load(std::memory_order_relaxed)));
        cam_info[cam->name] = status;
        cam->prev_input_frames = in;
        cam->prev_encoded_frames = encoded;
    }

    nlohmann::json clients = nlohmann::json::array();
    {
        std::lock_guard<std::mutex> lock(peers_mutex_);
        for (auto& kv : peers_) {
            Peer* p = kv.second.get();
            const std::string conn = conn_state_name(peer_connection_state(p->webrtc));
            const int64_t last = p->last_rtcp_ns.load(std::memory_order_relaxed);
            double rtcp_age = -1.0;
            if (last != 0) {
                rtcp_age = (now.time_since_epoch().count() - last) / 1e9;
            }
            // fps is a node-wide per-camera rate (the camera is encoded once); reported against each
            // peer that subscribes to it so the dashboard shows whether that stream is live.
            nlohmann::json streams = nlohmann::json::array();
            for (const auto& v : p->active) {  // report streams actually being sent, not merely negotiated
                auto info = cam_info.find(v);
                if (info == cam_info.end())
                    continue;
                nlohmann::json s;
                s["name"] = v;
                s["topic"] = info->second.topic;
                s["fps"] = round1(info->second.input_fps);
                s["input_fps"] = round1(info->second.input_fps);
                s["rtp_packet_fps"] = round1(info->second.rtp_packet_fps);
                s["encoding"] = info->second.rtp_packet_fps > 0.5;  // encoded RTP exists
                s["input_flow"] = info->second.input_flow;
                s["input_push_errors"] = info->second.input_push_errors;
                s["rtp_pushes"] = p->rtp_pushes[v];
                s["rtp_push_errors"] = p->rtp_push_errors[v];
                if (auto flow = p->rtp_flow.find(v); flow != p->rtp_flow.end()) {
                    s["rtp_flow"] = flow->second;
                }
                streams.push_back(s);
            }
            if (p->audio_active) {  // report audio only while it's actually being sent
                nlohmann::json s;
                s["name"] = "audio";
                s["encoding"] = audio_playing_;  // mic open + flowing
                s["rtp_pushes"] = p->rtp_pushes["audio"];
                s["rtp_push_errors"] = p->rtp_push_errors["audio"];
                if (auto flow = p->rtp_flow.find("audio"); flow != p->rtp_flow.end()) {
                    s["rtp_flow"] = flow->second;
                }
                streams.push_back(s);
            }
            nlohmann::json c;
            c["client_id"] = p->client_id;
            c["source"] = current_source_;
            c["audio"] = p->audio_active;
            c["connection_state"] = conn;
            c["media_ready"] = p->media_ready->load(std::memory_order_relaxed);
            if (rtcp_age >= 0.0) {
                c["rtcp_age_s"] = std::round(rtcp_age * 100.0) / 100.0;
            }
            c["streams"] = streams;
            clients.push_back(c);
        }
    }

    nlohmann::json root;
    root["count"] = clients.size();
    root["clients"] = clients;
    // The full set of configured cameras (m-line order), so clients can render a per-camera UI
    // dynamically instead of hardcoding main/arm.
    nlohmann::json cams = nlohmann::json::array();
    for (auto& cam : cameras_)
        cams.push_back(cam->name);
    root["cameras"] = cams;

    std_msgs::msg::String msg;
    msg.data = root.dump();
    active_streams_pub_->publish(msg);
}

}  // namespace mars_cam

RCLCPP_COMPONENTS_REGISTER_NODE(mars_cam::WebRTCStreamer)

#ifndef BUILDING_COMPONENT_LIBRARY
int main(int argc, char* argv[]) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<mars_cam::WebRTCStreamer>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
#endif
