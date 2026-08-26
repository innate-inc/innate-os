#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <gst/app/app.h>
#include <gst/rtp/rtp.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <sstream>

namespace mars_cam {

namespace {

// webrtcbin's recv jitterbuffer, in ms.
constexpr int kTalkJitterMs = 100;

// `a=sendrecv` on the audio m-line invites the browser to send its mic back on the SAME m-line — no
// extra m-line, no renegotiation. webrtcbin 1.20 already defaults to sendrecv here, but that is a
// default, not a contract: set it and verify it stuck, so an upgrade fails loudly instead of silently
// swallowing the operator's voice.
bool enable_talk_direction(GstElement* webrtc, guint mline) {
    GstWebRTCRTPTransceiver* trans = nullptr;
    g_signal_emit_by_name(webrtc, "get-transceiver", mline, &trans);
    if (!trans) {
        return false;
    }
    g_object_set(trans, "direction", GST_WEBRTC_RTP_TRANSCEIVER_DIRECTION_SENDRECV, nullptr);
    GstWebRTCRTPTransceiverDirection applied = GST_WEBRTC_RTP_TRANSCEIVER_DIRECTION_NONE;
    g_object_get(trans, "direction", &applied, nullptr);
    gst_object_unref(trans);
    return applied == GST_WEBRTC_RTP_TRANSCEIVER_DIRECTION_SENDRECV;
}

}  // namespace

// =============================================================================
// Per-peer transport
// =============================================================================

std::string WebRTCStreamer::build_transport_description(const std::vector<std::string>& videos,
                                                        bool& with_audio) const {
    // Declare the branches but DON'T link them to webrtcbin here: the RTP caps (incl. the extmap) are
    // set on each appsrc programmatically and the sink pads are requested/linked in order afterwards,
    // so webrtcbin sees the right caps at link time (linking with empty caps makes it collapse both
    // pads onto one transceiver -> the _create_offer_task seen_transceivers assertion).
    // latency= is webrtcbin's RECV jitterbuffer (default 200 ms). Only talkback receives, and 200 ms is
    // most of a mouth-to-speaker budget, so it is halved here.
    std::string desc = "webrtcbin name=webrtc bundle-policy=max-bundle latency=" + std::to_string(kTalkJitterMs) + " ";
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

Peer* WebRTCStreamer::create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                            const std::vector<std::string>& active, bool with_audio, bool audio_active,
                                            bool talk_active) {
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
    peer->audio_requested = audio_active;
    peer->created_ns = std::chrono::steady_clock::now().time_since_epoch().count();
    peer->webrtc = gst_bin_get_by_name(GST_BIN(pipeline), "webrtc");
    if (!peer->webrtc) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing webrtcbin");
        return nullptr;  // ~Peer() tears down the pipeline
    }

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
                                    G_TYPE_STRING, "2", "payload", G_TYPE_INT, 98, nullptr);
            if (link_rtp_appsrc(peer->webrtc, asrc, caps, 1 * 1024 * 1024)) {
                peer->rtp["audio"] = asrc;  // keep the ref
            } else {
                RCLCPP_WARN(this->get_logger(), "Failed to link audio appsrc; continuing video-only");
                gst_object_unref(asrc);
                peer->with_audio = false;
            }
        } else {
            peer->with_audio = false;
        }
    }
    // Talkback rides that same m-line in reverse: claim the recv direction, then take the operator's
    // mic off webrtcbin's pad-added (the gate rides element data because that callback and the decode
    // branch's sample callback run on streaming threads that must not take peers_mutex_).
    if (ok && peer->with_audio && enable_talkback_) {
        peer->with_talk = enable_talk_direction(peer->webrtc, static_cast<guint>(negotiated.size()));
        if (peer->with_talk) {
            peer->talk_gate->store(talk_active, std::memory_order_relaxed);
            g_object_set_data_full(G_OBJECT(peer->webrtc), "mars_talk_gate",
                                   new std::shared_ptr<std::atomic<bool>>(peer->talk_gate),
                                   [](gpointer p) { delete static_cast<std::shared_ptr<std::atomic<bool>>*>(p); });
            g_signal_connect(peer->webrtc, "pad-added", G_CALLBACK(on_talk_pad_added), this);
        } else {
            RCLCPP_WARN(this->get_logger(), "webrtcbin refused a sendrecv audio transceiver; talkback disabled");
        }
    }
    peer->talk_active = peer->with_talk && talk_active;
    // The robot's speaker is within earshot of its own mic, so without AEC the talker would hear their
    // own voice come back — half-duplex ducks the mic to them. With AEC the echo is subtracted at the
    // capture, so the duck (and the deafness it causes while talking) is lifted.
    const bool talk_duck = peer->talk_active && !enable_echo_cancel_;
    peer->audio_active = peer->with_audio && peer->audio_requested && !talk_duck;
    if (!ok) {
        RCLCPP_ERROR(this->get_logger(), "Transport pipeline missing/failed an rtp appsrc");
        return nullptr;  // ~Peer() tears down the pipeline + the rtp appsrcs stored so far
    }

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

    // Every failure return is behind us, so an attached mixer input can no longer be orphaned by one.
    // This is also before the offer is created below, and a peer cannot receive RTP before it has
    // offered — so the tag is always in place by the time pad-added fires.
    if (peer->with_talk && !attach_speaker_input(peer.get())) {
        RCLCPP_WARN(this->get_logger(), "No speaker input for peer '%s'; talkback disabled for it", client_id.c_str());
        peer->with_talk = false;
        peer->talk_active = false;
        peer->talk_gate->store(false, std::memory_order_relaxed);
        peer->audio_active = peer->with_audio && peer->audio_requested;  // nothing left to duck for
    }
    if (peer->with_talk) {
        g_object_set_data(G_OBJECT(peer->webrtc), "mars_talk_src", peer->talk_src);
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
    if (raw->talk_active) {
        want_talk_.fetch_add(1, std::memory_order_relaxed);
        reconcile_talk();
    }
    reconcile_subscriptions();  // this peer just negotiated its cameras — make sure they're subscribed

    on_negotiation_needed(raw->webrtc, this);
    RCLCPP_INFO(this->get_logger(), "Peer '%s' transport PLAYING (negotiated=%zu, active=%zu, audio=%s, talk=%s)",
                client_id.c_str(), negotiated.size(), active.size(),
                raw->audio_active ? "on" : (raw->with_audio ? "negotiated/off" : "off"),
                raw->talk_active ? "on" : (raw->with_talk ? "negotiated/off" : "off"));
    return raw;
}

void WebRTCStreamer::update_peer_active(Peer* peer, const std::vector<std::string>& active, bool audio_active,
                                        bool talk_active) {
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

    // Talkback toggles first: without AEC it ducks the outbound mic below, so the operator never hears
    // their own voice come back off the robot's speaker.
    const bool talk_now = peer->with_talk && talk_active;
    if (talk_now != peer->talk_active) {
        set_peer_talk(peer, talk_now);
        want_talk_.fetch_add(talk_now ? 1 : -1, std::memory_order_relaxed);
        reconcile_talk();
    }
    if (peer->talk_active)
        summary += (summary.empty() ? "" : "+") + std::string("talk");

    // Audio toggles the same way (only if the m-line was negotiated). Opening the mic here is safe under
    // the lock; closing it (want_audio_ -> 0) is deferred to the health poll, which runs without the lock,
    // because NULL-ing the audio pipeline joins the fan-out thread that also takes peers_mutex_.
    peer->audio_requested = audio_active;
    const bool audio_now = peer->with_audio && audio_active && !(peer->talk_active && !enable_echo_cancel_);
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

// The operator's mic arrived. Build this peer's playback branch and hang it off the new pad. Runs on
// webrtcbin's streaming thread: it must not take peers_mutex_ (~Peer NULLs the pipeline under that lock,
// which joins this very thread), so everything it needs comes off the element — the pipeline through the
// parent, the operator's current talk state through the tagged gate.
void WebRTCStreamer::on_talk_pad_added(GstElement* webrtc, GstPad* pad, gpointer user_data) {
    auto* self = static_cast<WebRTCStreamer*>(user_data);
    if (GST_PAD_DIRECTION(pad) != GST_PAD_SRC) {
        return;
    }
    GstCaps* caps = gst_pad_get_current_caps(pad);
    if (!caps) {
        caps = gst_pad_query_caps(pad, nullptr);
    }
    const GstStructure* st = caps ? gst_caps_get_structure(caps, 0) : nullptr;
    const gchar* media = st ? gst_structure_get_string(st, "media") : nullptr;
    const bool is_audio = media && g_strcmp0(media, "audio") == 0;
    if (caps) {
        gst_caps_unref(caps);
    }
    if (!is_audio) {
        return;  // we negotiate no recv video; anything else is not ours to play
    }
    // One branch per peer: the gate owns exactly one valve, so a second branch would play ungated. A
    // peer offers exactly once, so a second audio pad means an assumption changed — say so.
    if (g_object_get_data(G_OBJECT(webrtc), "mars_talk_attached")) {
        RCLCPP_WARN(self->get_logger(), "Second talkback audio pad on one peer; ignoring it");
        return;
    }

    GstElement* pipeline = GST_ELEMENT(gst_element_get_parent(webrtc));
    if (!pipeline) {
        return;
    }
    // This peer's own depayloader and decoder, in this peer's own pipeline — so its jitterbuffer's GAP
    // events still reach opusdec (that is what plc=true conceals loss with), and so two operators talking
    // at once never share a depayloader. Only the decoded PCM crosses into the shared speaker mixer.
    GError* error = nullptr;
    GstElement* branch = gst_parse_bin_from_description(talk_decode_description().c_str(), TRUE, &error);
    if (error || !branch) {
        RCLCPP_ERROR(self->get_logger(), "Talkback branch failed to build: %s", error ? error->message : "unknown");
        if (error) {
            g_error_free(error);
        }
        if (branch) {
            gst_object_unref(branch);
        }
        gst_object_unref(pipeline);
        return;
    }
    // The gate and this peer's mixer input ride on the appsink: on_talk_sample runs on this peer's
    // streaming thread, which must not reach into peers_ for either of them.
    auto* gate =
        static_cast<std::shared_ptr<std::atomic<bool>>*>(g_object_get_data(G_OBJECT(webrtc), "mars_talk_gate"));
    auto* talk_src = static_cast<GstElement*>(g_object_get_data(G_OBJECT(webrtc), "mars_talk_src"));
    if (GstElement* pcm_sink = gst_bin_get_by_name(GST_BIN(branch), "talk_pcm")) {
        if (gate) {
            g_object_set_data_full(G_OBJECT(pcm_sink), "mars_talk_gate", new std::shared_ptr<std::atomic<bool>>(*gate),
                                   [](gpointer p) { delete static_cast<std::shared_ptr<std::atomic<bool>>*>(p); });
        }
        g_object_set_data(G_OBJECT(pcm_sink), "mars_talk_src", talk_src);
        g_signal_connect(pcm_sink, "new-sample", G_CALLBACK(on_talk_sample), self);
        gst_object_unref(pcm_sink);
    }

    gst_bin_add(GST_BIN(pipeline), branch);
    gst_element_sync_state_with_parent(branch);
    GstPad* sink = gst_element_get_static_pad(branch, "sink");
    const bool linked = sink && gst_pad_link(pad, sink) == GST_PAD_LINK_OK;
    if (sink) {
        gst_object_unref(sink);
    }
    if (!linked) {
        RCLCPP_ERROR(self->get_logger(), "Failed to link the talkback branch to webrtcbin");
        gst_element_set_state(branch, GST_STATE_NULL);
        gst_bin_remove(GST_BIN(pipeline), branch);
        gst_object_unref(pipeline);
        return;
    }
    g_object_set_data(G_OBJECT(webrtc), "mars_talk_attached", GINT_TO_POINTER(1));
    RCLCPP_INFO(self->get_logger(), "Talkback branch attached (peer decodes into the speaker mixer)");
    gst_object_unref(pipeline);
}

void WebRTCStreamer::set_peer_talk(Peer* peer, bool on) {
    peer->talk_active = on;
    peer->talk_gate->store(on, std::memory_order_relaxed);
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
    if (p->talk_active) {
        set_peer_talk(p, false);  // a peer that vanishes mid-sentence goes quiet
        want_talk_.fetch_sub(1, std::memory_order_relaxed);
        reconcile_talk();
    }

    RCLCPP_INFO(this->get_logger(), "Released peer '%s'", client_id.c_str());
    // Taken before the erase, released after it: ~Peer NULLs the transport (joining the streaming thread
    // that pushes into this appsrc), so only then is pulling its mixer input out of the speaker safe.
    GstElement* talk_src = p->talk_src;
    GstPad* talk_mix_pad = p->talk_mix_pad;
    peers_.erase(it);  // ~Peer() NULLs the transport (joining its streaming threads) and releases the refs
    detach_speaker_input(talk_src, talk_mix_pad);
    reconcile_subscriptions();  // last peer wanting a camera may have just left — drop its sub if so
}

}  // namespace mars_cam
