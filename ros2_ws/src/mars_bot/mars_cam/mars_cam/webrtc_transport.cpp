#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <gst/app/app.h>
#include <gst/rtp/rtp.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <sstream>

namespace mars_cam {

namespace {
struct KeyUnitCtx {
    WebRTCStreamer* self;
    std::string cam;
};

struct TransceiverCtx {
    WebRTCStreamer* self;
    guint video_count;  // transceivers are created in sink-pad link order: the video m-lines, then audio
    guint seen = 0;
};
}  // namespace

// =============================================================================
// Per-peer transport
// =============================================================================

// webrtcbin turns an incoming PLI into a GstForceKeyUnit upstream event, but that event dies at the
// transport appsrc — the encoder lives in a separate pipeline — so without this probe the browser
// cannot request recovery and every loss waits out the periodic keyframe backstop.
GstPadProbeReturn WebRTCStreamer::on_keyunit_request(GstPad*, GstPadProbeInfo* info, gpointer user_data) {
    GstEvent* ev = gst_pad_probe_info_get_event(info);
    if (!ev || GST_EVENT_TYPE(ev) != GST_EVENT_CUSTOM_UPSTREAM) {
        return GST_PAD_PROBE_OK;
    }
    const GstStructure* s = gst_event_get_structure(ev);
    if (!s || !gst_structure_has_name(s, "GstForceKeyUnit")) {
        return GST_PAD_PROBE_OK;
    }
    auto* ctx = static_cast<KeyUnitCtx*>(user_data);
    ctx->self->maybe_force_keyframe(ctx->cam);
    return GST_PAD_PROBE_DROP;  // consumed; the appsrc has no use for it
}

std::string WebRTCStreamer::build_transport_description(const std::vector<std::string>& videos,
                                                        bool& with_audio) const {
    // Declare the branches but DON'T link them to webrtcbin here: the RTP caps (incl. the extmap) are
    // set on each appsrc programmatically and the sink pads are requested/linked in order afterwards,
    // so webrtcbin sees the right caps at link time (linking with empty caps makes it collapse both
    // pads onto one transceiver -> the _create_offer_task seen_transceivers assertion).
    std::string desc = "webrtcbin name=webrtc bundle-policy=max-bundle ";
    for (const auto& v : videos) {
        desc += "appsrc name=rtp_" + v + " is-live=true format=time do-timestamp=false ";
    }
    // Audio is encoded ONCE in the shared mic pipeline and fanned out, so the transport just needs an
    // appsrc m-line; its opus RTP caps are set programmatically in create_peer_transport (before linking).
    if (with_audio) {
        desc += "appsrc name=rtp_audio is-live=true format=time do-timestamp=false ";
    }
    return desc;
}

// Apply RTP caps + live/leaky tuning to a transport appsrc, then request the next webrtcbin sink pad and
// link it. Consumes `caps`. Returns true once linked (the caller keeps the appsrc ref in peer->rtp).
// do-timestamp=TRUE re-stamps each forwarded buffer with this transport pipeline's running-time: the
// encode pipeline is separate with its own base-time, so its PTS look future-dated here and webrtcbin
// would hold them. The RTP header timestamps the receiver plays back by are the payloader's, untouched.
bool WebRTCStreamer::link_rtp_appsrc(GstElement* webrtc, GstElement* appsrc, GstCaps* caps, guint64 max_bytes) {
    g_object_set(appsrc, "caps", caps, "is-live", TRUE, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "block", FALSE,
                 "leaky-type", 2 /* downstream */, "max-bytes", max_bytes, nullptr);
    gst_caps_unref(caps);
    GstPad* srcpad = gst_element_get_static_pad(appsrc, "src");
    GstPad* sinkpad = gst_element_request_pad_simple(webrtc, "sink_%u");
    const bool linked = srcpad && sinkpad && gst_pad_link(srcpad, sinkpad) == GST_PAD_LINK_OK;
    if (srcpad)
        gst_object_unref(srcpad);
    if (sinkpad)
        gst_object_unref(sinkpad);
    return linked;
}

// Post-link re-assert of the creation-time loss-repair props (the on-new-transceiver hook in
// create_peer_transport is where they take effect): the first `video_count` transceivers get
// NACK/RTX + ULPFEC/RED, anything after (audio) is stripped. fec-percentage defaults to 100
// (double the bitrate) — always set it alongside fec-type.
void WebRTCStreamer::configure_video_transceivers(GstElement* webrtc, size_t video_count) {
    for (size_t i = 0;; ++i) {
        GstWebRTCRTPTransceiver* trans = nullptr;
        g_signal_emit_by_name(webrtc, "get-transceiver", static_cast<gint>(i), &trans);
        if (!trans) {
            if (i < video_count) {
                RCLCPP_WARN(this->get_logger(), "No transceiver for video m-line %zu; loss repair not applied", i);
            }
            return;
        }
        const bool video = i < video_count;
        g_object_set(trans, "do-nack", (video && video_nack_) ? TRUE : FALSE, nullptr);
        const guint pct = (video && video_fec_percentage_ > 0) ? current_fec_pct_.load(std::memory_order_relaxed) : 0u;
        g_object_set(trans, "fec-type", pct > 0 ? GST_WEBRTC_FEC_TYPE_ULP_RED : GST_WEBRTC_FEC_TYPE_NONE,
                     "fec-percentage", pct, nullptr);
        g_object_unref(trans);
    }
}

Peer* WebRTCStreamer::create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                            const std::vector<std::string>& active, bool with_audio,
                                            bool audio_active) {
    if (client_id.empty()) {
        RCLCPP_WARN(this->get_logger(), "Refusing to create WebRTC peer without client_id");
        return nullptr;
    }
    destroy_peer(client_id);  // replace any existing peer with this id (re-START)

    std::string desc = build_transport_description(negotiated, with_audio);
    GError* error = nullptr;
    GstElement* pipeline = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create transport pipeline (audio=%s): %s",
                     with_audio ? "on" : "off", error->message);
        g_error_free(error);
        if (pipeline) {
            gst_object_unref(pipeline);
        }
        return nullptr;
    }

    auto peer = std::make_unique<Peer>();
    peer->client_id = client_id;
    peer->pipeline = pipeline;
    peer->videos = negotiated;
    peer->active = active;
    peer->with_audio = with_audio;
    peer->audio_active = with_audio && audio_active;  // can only be active if the m-line was negotiated
    peer->created_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    peer->webrtc = gst_bin_get_by_name(GST_BIN(pipeline), "webrtc");
    if (!peer->webrtc) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing webrtcbin");
        return nullptr;  // ~Peer() tears down the pipeline
    }

    // Loss-repair props must land the moment each transceiver is CREATED (as its sink pad links):
    // webrtcbin wires its internal FEC/RTX elements from them during negotiation setup, so a
    // post-link write cannot engage them. A pad-created transceiver doesn't know its media kind
    // yet, but creation order is link order — the first `negotiated.size()` are the video m-lines,
    // so audio never gets video loss-repair wired in the first place.
    auto* tctx = new TransceiverCtx{this, static_cast<guint>(negotiated.size())};
    g_signal_connect_data(
        peer->webrtc, "on-new-transceiver",
        G_CALLBACK(+[](GstElement*, GstWebRTCRTPTransceiver* trans, gpointer user_data) {
            auto* ctx = static_cast<TransceiverCtx*>(user_data);
            if (ctx->seen++ >= ctx->video_count) {
                return;  // audio m-line
            }
            auto* self = ctx->self;
            g_object_set(trans, "do-nack", self->video_nack_ ? TRUE : FALSE, nullptr);
            if (self->video_fec_percentage_ > 0) {
                // A peer arriving mid-rung gets the fleet's current protection from the start.
                g_object_set(trans, "fec-type", GST_WEBRTC_FEC_TYPE_ULP_RED, "fec-percentage",
                             self->current_fec_pct_.load(std::memory_order_relaxed), nullptr);
            }
        }),
        tctx, [](gpointer data, GClosure*) { delete static_cast<TransceiverCtx*>(data); },
        static_cast<GConnectFlags>(0));

    // Configure each RTP appsrc: caps (incl. the extmap so webrtcbin emits a=extmap), drop-old on
    // congestion so one slow peer can't backpressure the shared encoders.
    const std::string extmap_field = "extmap-" + std::to_string(kPlayoutDelayExtId);
    bool ok = true;
    for (const auto& v : negotiated) {
        CameraEncoder* c = find_camera(v);
        GstElement* src = c ? gst_bin_get_by_name(GST_BIN(pipeline), ("rtp_" + v).c_str()) : nullptr;
        if (!c || !src) {
            if (src)
                gst_object_unref(src);
            ok = false;
            break;
        }
        GstCaps* caps = gst_caps_new_simple("application/x-rtp", "media", G_TYPE_STRING, "video", "encoding-name",
                                            G_TYPE_STRING, "VP8", "clock-rate", G_TYPE_INT, 90000, "payload",
                                            G_TYPE_INT, c->pt, "ssrc", G_TYPE_UINT, c->ssrc, nullptr);
        gst_caps_set_simple(caps, extmap_field.c_str(), G_TYPE_STRING, MARS_PLAYOUT_DELAY_URI, nullptr);
        // Set the (VP8) caps, then request the next webrtcbin sink pad and link — so the transceiver is
        // built from the real caps, in m-line order (sink_0, sink_1, …).
        if (!link_rtp_appsrc(peer->webrtc, src, caps, 2 * 1024 * 1024)) {
            RCLCPP_ERROR(this->get_logger(), "Failed to link rtp_%s to webrtcbin", v.c_str());
            gst_object_unref(src);
            ok = false;
            break;
        }
        if (GstPad* pad = gst_element_get_static_pad(src, "src")) {
            gst_pad_add_probe(pad, GST_PAD_PROBE_TYPE_EVENT_UPSTREAM, on_keyunit_request, new KeyUnitCtx{this, v},
                              [](gpointer p) { delete static_cast<KeyUnitCtx*>(p); });
            gst_object_unref(pad);
        }
        peer->rtp[v] = src;  // keep the ref (camera name -> transport appsrc)
    }
    // Audio (if negotiated): set the opus RTP caps on the audio appsrc (caps-before-link, same as video)
    // and link it as the LAST m-line. The shared mic pipeline fans its RTP into this appsrc.
    if (ok && peer->with_audio) {
        GstElement* asrc = gst_bin_get_by_name(GST_BIN(pipeline), "rtp_audio");
        if (asrc) {
            GstCaps* caps =
                gst_caps_new_simple("application/x-rtp", "media", G_TYPE_STRING, "audio", "encoding-name",
                                    G_TYPE_STRING, "OPUS", "clock-rate", G_TYPE_INT, 48000, "encoding-params",
                                    G_TYPE_STRING, "2", "payload", G_TYPE_INT, kAudioPt, nullptr);
            if (link_rtp_appsrc(peer->webrtc, asrc, caps, 1 * 1024 * 1024)) {
                peer->rtp["audio"] = asrc;  // keep the ref
            } else {
                RCLCPP_WARN(this->get_logger(), "Failed to link audio appsrc; continuing video-only");
                gst_object_unref(asrc);
                peer->with_audio = false;
                peer->audio_active = false;
            }
        } else {
            peer->with_audio = false;
            peer->audio_active = false;
        }
    }
    if (!ok) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing/failed an rtp appsrc");
        return nullptr;  // ~Peer() tears down the pipeline + the rtp appsrcs stored so far
    }
    configure_video_transceivers(peer->webrtc, negotiated.size());

    // Tag the webrtcbin with its client_id (ICE-candidate routing) and a copy of its generation token,
    // so the on-negotiation-needed handler can build the offer context lock-free off the element alone.
    g_object_set_data_full(G_OBJECT(peer->webrtc), "client_id", g_strdup(client_id.c_str()), g_free);
    g_object_set_data_full(G_OBJECT(peer->webrtc), "mars_gen",
                           new std::shared_ptr<std::atomic<uint64_t>>(peer->generation),
                           [](gpointer p) { delete static_cast<std::shared_ptr<std::atomic<uint64_t>>*>(p); });
    // Same lock-free pattern for media_ready: the connection-state callback fires on webrtcbin's PC
    // thread, where taking peers_mutex_ would deadlock against ~Peer (set_state(NULL) under the mutex
    // joins that thread). The shared atomic outlives the Peer, so a late notify is harmless.
    g_object_set_data_full(G_OBJECT(peer->webrtc), "mars_media_ready",
                           new std::shared_ptr<std::atomic<bool>>(peer->media_ready),
                           [](gpointer p) { delete static_cast<std::shared_ptr<std::atomic<bool>>*>(p); });
    g_object_set_data(G_OBJECT(peer->webrtc), "mars_expected_videos",
                      GUINT_TO_POINTER(static_cast<guint>(negotiated.size())));
    g_object_set_data(G_OBJECT(peer->webrtc), "mars_expected_audio", GINT_TO_POINTER(peer->with_audio ? 1 : 0));
    // Offer-once latch: CAS'd 0->1 by whichever of the racing negotiation-needed callers wins (the explicit
    // one below on the executor thread vs. webrtcbin's queued signal on the GLib thread). A second create-offer
    // would renegotiate and disrupt the live connection; the atomic prevents it.
    g_object_set_data_full(G_OBJECT(peer->webrtc), "mars_offer_latch", new std::atomic<int>(0),
                           [](gpointer p) { delete static_cast<std::atomic<int>*>(p); });
    g_signal_connect(peer->webrtc, "on-ice-candidate", G_CALLBACK(on_ice_candidate), this);
    g_signal_connect(peer->webrtc, "notify::connection-state", G_CALLBACK(on_connection_state_changed), this);

    if (GstObject* ice = nullptr; (g_object_get(peer->webrtc, "ice-agent", &ice, nullptr), ice)) {
        g_object_set(ice, "ice-tcp", FALSE, nullptr);  // UDP-only media: fewer candidate pairs to check
        // NOTE: we deliberately leave the NiceAgent's STUN retransmit budget at libnice defaults. A short
        // budget (stun-initial-timeout=100, stun-max-retransmissions=2 → ~0.7 s give-up) abandons dead pairs
        // faster, but it also makes libnice declare the component FAILED before a peer-reflexive pair can be
        // nominated — which is exactly what happens when the browser's host candidates are unresolvable mDNS
        // names (e.g. a Linux client not publishing them) and the ONLY way in is the prflx that libnice
        // discovers from the browser's incoming connectivity checks. Default (patient) checking keeps the
        // agent alive long enough for that prflx pair to win. Dead pairs cost a few extra seconds; a dead
        // *connection* costs everything.
        gst_object_unref(ice);
    }
    GstStateChangeReturn ret = gst_element_set_state(pipeline, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_ASYNC) {
        ret = gst_element_get_state(pipeline, nullptr, nullptr, 3 * GST_SECOND);
    }
    if (ret == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline failed to reach PLAYING (audio=%s)",
                     with_audio ? "on" : "off");
        return nullptr;  // ~Peer() tears down the pipeline
    }

    // Keep listening for future negotiation-needed signals, but create the first offer explicitly after all
    // media transceivers are in place.
    g_signal_connect(peer->webrtc, "on-negotiation-needed", G_CALLBACK(on_negotiation_needed), this);

    Peer* raw = peer.get();
    peers_[client_id] = std::move(peer);
    // want-count gates the encoders; count only ACTIVE (pushed) cameras, not merely negotiated ones, so a
    // peer that negotiated several but is viewing one doesn't pin the others' encoders on.
    for (const auto& v : active) {
        if (CameraEncoder* c = find_camera(v))
            c->want.fetch_add(1, std::memory_order_relaxed);
    }
    if (raw->audio_active) {
        want_audio_.fetch_add(1, std::memory_order_relaxed);
        reconcile_audio();  // opens the mic — safe under the lock (opening never joins the fan-out thread)
    }
    reconcile_subscriptions();  // this peer just negotiated its cameras — make sure they're subscribed

    on_negotiation_needed(raw->webrtc, this);
    RCLCPP_INFO(this->get_logger(), "Peer '%s' transport PLAYING (negotiated=%zu, active=%zu, audio=%s)",
                client_id.c_str(), negotiated.size(), active.size(),
                raw->audio_active ? "on" : (raw->with_audio ? "negotiated/off" : "off"));
    return raw;
}

void WebRTCStreamer::update_peer_active(Peer* peer, const std::vector<std::string>& active, bool audio_active) {
    // Toggle the pushed cameras on a peer whose transceivers are already negotiated — no offer/answer, no
    // ICE, so a stream switch is instant instead of a full reconnect. Only cameras the peer negotiated can
    // be enabled; anything else would need renegotiation and is ignored here.
    std::vector<std::string> next;
    for (const auto& v : active) {
        if (wants(peer->videos, v) && !wants(next, v)) {
            next.push_back(v);  // only negotiated cameras can be enabled without renegotiating
        }
    }

    std::vector<std::string> newly_enabled;
    std::string summary;
    for (auto& c : cameras_) {
        const bool was = wants(peer->active, c->name);
        const bool now = wants(next, c->name);
        if (now && !was) {
            c->want.fetch_add(1, std::memory_order_relaxed);
            newly_enabled.push_back(c->name);
        } else if (!now && was) {
            c->want.fetch_sub(1, std::memory_order_relaxed);
        }
        if (now)
            summary += (summary.empty() ? "" : "+") + c->name;
    }
    peer->active = next;

    // Audio toggles the same way (only if the m-line was negotiated). Opening the mic here is safe under
    // the lock; closing it (want_audio_ -> 0) is deferred to the health poll, which runs without the lock,
    // because NULL-ing the audio pipeline joins the fan-out thread that also takes peers_mutex_.
    const bool audio_now = peer->with_audio && audio_active;
    if (audio_now != peer->audio_active) {
        if (audio_now) {
            want_audio_.fetch_add(1, std::memory_order_relaxed);
            peer->audio_active = true;
            reconcile_audio();  // open the mic now
        } else {
            want_audio_.fetch_sub(1, std::memory_order_relaxed);
            peer->audio_active = false;  // reconcile_audio() (poll_pipeline_health) closes the mic if 0
        }
    }
    if (peer->audio_active)
        summary += (summary.empty() ? "" : "+") + std::string("audio");

    // Force an IDR on each newly-enabled camera so the browser's existing (idle) transceiver decodes the
    // resumed stream within a frame instead of waiting for the next periodic keyframe.
    for (const auto& cam : newly_enabled) {
        force_keyframe(cam);
    }

    RCLCPP_INFO(this->get_logger(), "Peer '%s' active streams -> [%s] (no reneg)", peer->client_id.c_str(),
                summary.empty() ? "none" : summary.c_str());
}

void WebRTCStreamer::on_negotiation_needed(GstElement* webrtc, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    const char* cid = static_cast<const char*>(g_object_get_data(G_OBJECT(webrtc), "client_id"));
    auto* genp = static_cast<std::shared_ptr<std::atomic<uint64_t>>*>(g_object_get_data(G_OBJECT(webrtc), "mars_gen"));
    if (!genp) {
        return;
    }
    // Offer exactly once per peer: a later renegotiation (e.g. when media starts) must not re-offer and
    // disrupt a live connection. The explicit call (executor thread) and webrtcbin's queued signal (GLib
    // thread) race here, so the latch is an atomic CAS — exactly one caller wins and emits create-offer.
    auto* latch = static_cast<std::atomic<int>*>(g_object_get_data(G_OBJECT(webrtc), "mars_offer_latch"));
    if (int expected = 0; !latch || !latch->compare_exchange_strong(expected, 1, std::memory_order_acq_rel)) {
        return;
    }

    const std::string client_id = cid ? cid : "";
    auto* ctx = new OfferContext{
        self,
        GST_ELEMENT(gst_object_ref(webrtc)),
        *genp,
        (*genp)->load(),
        client_id,
        static_cast<guint>(GPOINTER_TO_UINT(g_object_get_data(G_OBJECT(webrtc), "mars_expected_videos"))),
        static_cast<bool>(GPOINTER_TO_INT(g_object_get_data(G_OBJECT(webrtc), "mars_expected_audio")))};
    GstPromise* promise = gst_promise_new_with_change_func(on_offer_created, ctx, offer_context_free);
    g_signal_emit_by_name(webrtc, "create-offer", nullptr, promise);
    RCLCPP_INFO(self->get_logger(), "Negotiation needed for '%s'; offering...", client_id.c_str());
}

void WebRTCStreamer::destroy_peer(const std::string& client_id) {
    auto it = peers_.find(client_id);
    if (it == peers_.end()) {
        return;
    }
    Peer* p = it->second.get();
    p->generation->fetch_add(1, std::memory_order_relaxed);  // invalidate any in-flight offer
    // Mirror create/update: the want-count tracks ACTIVE streams, so release exactly what this peer held.
    for (const auto& v : p->active) {
        if (CameraEncoder* c = find_camera(v))
            c->want.fetch_sub(1, std::memory_order_relaxed);
    }
    if (p->audio_active)
        want_audio_.fetch_sub(1, std::memory_order_relaxed);  // mic closed by the health poll

    RCLCPP_INFO(this->get_logger(), "Released peer '%s'", client_id.c_str());
    peers_.erase(it);           // ~Peer() NULLs the transport (joining its streaming threads) and releases the refs
    reconcile_subscriptions();  // last peer wanting a camera may have just left — drop its sub if so
}

}  // namespace mars_cam
