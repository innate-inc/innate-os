// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
#ifndef MARS_CAM__WEBRTC_STREAMER_HPP_
#define MARS_CAM__WEBRTC_STREAMER_HPP_

#define GST_USE_UNSTABLE_API

#include "mars_cam/webrtc_config.hpp"

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <std_msgs/msg/string.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>

#include <gst/gst.h>
#include <gst/webrtc/webrtc.h>
#include <gst/sdp/sdp.h>

#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>

#include <string>
#include <vector>
#include <memory>
#include <map>
#include <mutex>
#include <chrono>
#include <functional>
#include <atomic>
#include <cstdint>
#include <thread>

namespace mars_cam {

class WebRTCStreamer;  // CameraEncoder back-references the node for the static appsink callback

// One persistently-encoded camera: `appsrc -> videoconvert -> vp8enc -> rtpvp8pay -> appsink`, built once
// in the constructor and PLAYING for the node's life. The appsink is the fan-out tap — every connected
// peer that has this camera active is fed from it, so each camera is encoded exactly once regardless of
// peer count. Burns zero CPU at idle (the image callback early-returns until `want > 0`) and the image
// topic is subscribed lazily (only while some peer negotiates it). The node supports N of these; `main`
// and `arm` are just the default two.
struct CameraEncoder {
    std::string name;
    std::string live_topic, replay_topic;
    int pt = 96;      // RTP payload type, unique per camera (see cam_pt_for_index)
    int fps = 30;     // encoder framerate (appsrc caps)
    int width = 640;  // encode resolution (appsrc caps); incoming frames are resized to it
    int height = 480;
    int bitrate_kbps = 2000;  // vp8enc target-bitrate
    guint ssrc = 0;           // fixed SSRC so the SDP offer carries a=ssrc/msid before any RTP has flowed
    std::atomic<int64_t> last_forced_ns{0};  // maybe_force_keyframe throttle (browser PLIs can storm)

    GstElement* appsrc = nullptr;  // src_<name>, ref'd out of the encode pipeline
    GstElement* sink = nullptr;    // sink_<name> appsink, ref'd; the fan-out tap
    GstBufferPool* pool = nullptr;
    std::atomic<int> want{0};  // # peers actively pushing this camera; 0 => the callback does no work
    std::atomic<uint64_t> input_frames{0};
    std::atomic<uint64_t> input_push_errors{0};
    std::atomic<uint64_t> encoded_frames{0};
    uint64_t prev_input_frames = 0;    // publish_status fps sampling
    uint64_t prev_encoded_frames = 0;  // publish_status fps sampling
    std::atomic<int> input_flow_code{GST_FLOW_OK};
    GstClockTime pts_base_ns = GST_CLOCK_TIME_NONE;  // capture-stamp anchor; PTS = frame stamp - this
    GstClockTime last_pts_ns = 0;                    // last emitted PTS (monotonic guard / unstamped tick)
    GstClockTime last_stamp_ns = 0;                  // last capture stamp seen (0 = none); backward step re-anchors
    rclcpp::SubscriptionBase::SharedPtr sub;         // lazy raw|compressed image subscription
    WebRTCStreamer* owner = nullptr;                 // for the static appsink new-sample callback
};

// One connected WebRTC peer. The cameras are encoded ONCE in a persistent pipeline; each peer owns a
// lightweight *transport* pipeline (appsrc(RTP) -> webrtcbin) that the shared encoders fan their RTP
// out to. Creating/destroying a peer never touches the encoders, so memory stays flat and a dead peer
// can't take the cameras down with it.
struct Peer {
    std::string client_id;
    GstElement* pipeline = nullptr;          // transport pipeline (owns webrtcbin + rtp appsrcs)
    GstElement* webrtc = nullptr;            // ref'd from pipeline
    std::map<std::string, GstElement*> rtp;  // camera name -> ref'd transport appsrc (the negotiated set)
    std::vector<std::string> videos;         // NEGOTIATED video streams (transceivers), in m-line order
    std::vector<std::string> active;         // currently PUSHED streams (subset of videos); toggled live on a
                                             // stream switch without renegotiating, so switches are instant
    bool with_audio = false;                 // audio m-line NEGOTIATED (an opus transceiver exists)
    bool audio_active = false;               // audio currently being SENT — toggled live like the cameras (no reneg)
    // True only after webrtcbin CONNECTED; gates RTP fan-out into webrtcbin. Shared + atomic because it
    // is written from webrtcbin's PC thread (the connection-state callback, via a copy tagged on the
    // element) — that callback must NOT take peers_mutex_: ~Peer runs set_state(NULL) under the mutex,
    // which joins the PC thread, so locking there is an ABBA deadlock that freezes every client.
    std::shared_ptr<std::atomic<bool>> media_ready = std::make_shared<std::atomic<bool>>(false);
    bool have_real_remote_ice = false;  // true after a non-mDNS candidate (local-STUN srflx fast path)
    int64_t first_mdns_ice_ns = 0;      // steady-clock ns when the first deferred .local candidate arrived
    std::vector<std::pair<int, std::string>> pending_mdns_ice;  // mline + raw candidate fallback

    // Stale-offer guard: shared with the async create-offer callback so it can tell its peer was torn
    // down / replaced underneath it (lock-free, and survives the Peer being freed). Bumped on destroy.
    std::shared_ptr<std::atomic<uint64_t>> generation = std::make_shared<std::atomic<uint64_t>>(0);

    // Steady-clock ns when the transport was created; a peer that never reaches CONNECTED within the
    // connect timeout (failed ICE, abandoned handshake) is released so it can't leak its transport.
    int64_t created_ns = 0;
    bool ever_connected = false;

    // Per-peer RTCP-inactivity watchdog (a vanished peer leaves webrtcbin stuck in CONNECTED).
    std::atomic<int64_t> last_rtcp_ns{0};  // steady-clock ns of last RTCP; 0 = disarmed
    bool rtcp_probe_installed = false;
    int terminal_polls = 0;                           // consecutive FAILED/DISCONNECTED health polls
    std::map<std::string, uint64_t> rtp_pushes;       // stream name -> buffers pushed into this transport
    std::map<std::string, uint64_t> rtp_push_errors;  // stream name -> non-OK appsrc push returns
    std::map<std::string, std::string> rtp_flow;      // stream name -> last GstFlowReturn name

    Peer() = default;
    Peer(const Peer&) = delete;  // owns raw GStreamer refs — never copy (would double-unref)
    Peer& operator=(const Peer&) = delete;
    // RAII teardown: NULL first (joins the transport's streaming threads, so no probe/callback runs after),
    // then release the refs. Destroying a peer is now just letting its unique_ptr go — every former manual
    // teardown (3 create-error paths + destroy_peer) collapses to this.
    ~Peer() {
        if (pipeline)
            gst_element_set_state(pipeline, GST_STATE_NULL);
        for (auto& kv : rtp)
            gst_object_unref(kv.second);
        if (webrtc)
            gst_object_unref(webrtc);
        if (pipeline)
            gst_object_unref(pipeline);
    }
};

class WebRTCStreamer : public rclcpp::Node {
   public:
    explicit WebRTCStreamer(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());
    ~WebRTCStreamer();

   private:
    // ---- Signaling (ROS). START/answer/ICE all carry an explicit client_id. ----
    void on_start(const std_msgs::msg::String::SharedPtr msg);
    void on_answer_id(const std_msgs::msg::String::SharedPtr msg);  // {client_id, sdp}
    void on_ice_in_id(const std_msgs::msg::String::SharedPtr msg);  // {client_id, candidate, ...}
    void deliver_answer(const std::string& client_id, const std::string& sdp);
    void deliver_ice(const std::string& client_id, const std::string& candidate, int mline);
    void apply_answer(Peer* peer, const std::string& sdp);                // caller holds peers_mutex_
    void apply_ice(Peer* peer, const std::string& candidate, int mline);  // caller holds peers_mutex_

    // ---- Camera frames -> persistent encoders (one generic handler per encoding, bound per camera) ----
    void on_image_raw(CameraEncoder* cam, const sensor_msgs::msg::Image::SharedPtr& msg);
    void on_image_compressed(CameraEncoder* cam, const sensor_msgs::msg::CompressedImage::SharedPtr& msg);

    // ---- GStreamer callbacks (static for the C callback interface) ----
    static void on_ice_candidate(GstElement* webrtc, guint mline, gchar* candidate, gpointer user_data);
    // Upstream-event probe on each transport rtp appsrc: forwards the browser's PLI-driven
    // GstForceKeyUnit to the (separate) encode pipeline, which the event can't reach on its own.
    static GstPadProbeReturn on_keyunit_request(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    static void on_connection_state_changed(GstElement* webrtc, GParamSpec* pspec, gpointer user_data);
    static void on_negotiation_needed(GstElement* webrtc, gpointer user_data);
    static void on_offer_created(GstPromise* promise, gpointer user_data);
    // appsink new-sample (user_data = the CameraEncoder*): pull the encoded RTP buffer and fan it out to
    // every peer with this camera active.
    static GstFlowReturn on_sample(GstElement* appsink, gpointer user_data);
    // Shared fan-out core (video + audio): copy the just-pulled sample to each peer appsrc that `select`
    // picks. `select` runs with peers_mutex_ held and returns the peer's transport appsrc, or nullptr to skip.
    void fan_out(GstElement* appsink, const std::function<GstElement*(Peer*)>& select, const std::string& stream);
    void fan_out_sample(GstElement* appsink, const std::string& cam);

    // ---- Shared audio (mic) — encoded once like the cameras, fanned out, gated for privacy ----
    bool build_audio_pipeline();  // alsasrc -> opusenc -> rtpopuspay -> appsink (built once, kept NULL)
    void reconcile_audio();       // PLAYING when some peer has audio active, NULL otherwise (mic off)
    static GstFlowReturn on_audio_sample(GstElement* appsink, gpointer user_data);
    void fan_out_audio(GstElement* appsink);

    // Per-peer RTCP-liveness probe on the peer's rtpbin recv_rtcp_sink pad (ticks per RTCP packet).
    static GstPadProbeReturn on_rtcp_buffer(GstPad* pad, GstPadProbeInfo* info, gpointer user_data);
    bool install_rtcp_probe_for(Peer* peer);  // attach once the peer's rtcp pad exists

    // ---- Persistent encode pipeline (built once in the constructor) ----
    void configure_cameras();  // read the camera list + per-camera params into cameras_
    std::string video_encode_branch(const CameraEncoder& cam) const;
    bool build_encode_pipeline();
    void attach_playout_delay_extension(const std::string& cam);  // adds the ext to one payloader
    cv::Mat process_raw_image(const sensor_msgs::msg::Image::SharedPtr& msg, int w, int h);
    cv::Mat process_compressed_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg, int w, int h);
    void push_frame(CameraEncoder* cam, const cv::Mat& frame, const rclcpp::Time& stamp);
    GstBufferPool* create_frame_pool(int width, int height, int channels);
    void force_keyframe(const std::string& cam);          // request an IDR so a fresh/resumed peer can decode
    void maybe_force_keyframe(const std::string& cam);    // force_keyframe, throttled per camera (PLI path)
    CameraEncoder* find_camera(const std::string& name);  // configured camera by name, or nullptr

    // ---- Per-peer transport ----
    // create_peer_transport builds the transport pipeline, wires signals, and kicks off create-offer.
    // Caller holds peers_mutex_. Returns the inserted Peer* (or nullptr on failure).
    Peer* create_peer_transport(const std::string& client_id, const std::vector<std::string>& negotiated,
                                const std::vector<std::string>& active, bool with_audio, bool audio_active);
    // Toggle which negotiated cameras (and audio) a connected peer is being sent, with NO renegotiation:
    // adjusts the per-camera/audio want-count + push gate and forces a keyframe on newly-enabled cameras.
    // Caller holds peers_mutex_.
    void update_peer_active(Peer* peer, const std::vector<std::string>& active, bool audio_active);
    void destroy_peer(const std::string& client_id);  // caller holds peers_mutex_
    std::string build_transport_description(const std::vector<std::string>& videos, bool& with_audio) const;
    // Apply RTP caps + tuning to a transport appsrc and link it to the next webrtcbin sink pad (consumes
    // caps). Shared by the video and audio m-line setup in create_peer_transport.
    bool link_rtp_appsrc(GstElement* webrtc, GstElement* appsrc, GstCaps* caps, guint64 max_bytes);
    // Enable NACK/RTX + ULPFEC (per video_nack_ / video_fec_percentage_) on the first `video_count`
    // transceivers — GStreamer's defaults negotiate no loss repair at all.
    void configure_video_transceivers(GstElement* webrtc, size_t video_count);
    void publish_offer(const std::string& client_id, const std::string& sdp);

    // ---- Camera subscriptions (lazy: a camera is subscribed only while some connected peer negotiates
    // it, and dropped when no peer does — so an idle node with no peers receives no camera frames at all).
    void reconcile_subscriptions();  // subscribe/unsubscribe each camera to match what peers negotiate
    void set_camera_subscribed(CameraEncoder* cam, bool subscribed);
    void destroy_subscriptions();

    // ---- Health / status (executor thread) ----
    void poll_pipeline_health();  // per-peer teardown of dead transports
    void publish_status();        // /webrtc/active_streams snapshot at 0.5 Hz

    // ---- RTCP-driven sender adaptation (executor thread + webrtcbin stats callbacks) ----
    // The viewers' RTCP receiver reports carry loss + RTT back to us; a 1 Hz poll turns the worst
    // of them into two states. GOOD = full bitrate, keyframes on demand only (lowest latency).
    // DEGRADED = ~60% bitrate + a periodic 1 Hz keyframe, so reference-chain corruption on an
    // unrepairable link clears in bounded time instead of smearing until a PLI round-trip.
    void poll_network_adaptation();
    static void on_peer_stats(GstPromise* promise, gpointer user_data);   // parses one get-stats reply
    void apply_adaptation(bool degraded, int loss_promille, int rtt_ms);  // sets vp8enc bitrates + logs

    // ---- Local STUN helper ----
    // Minimal RFC 8489 Binding responder. This gives browsers a robot-local STUN server so their srflx
    // candidate is the LAN IP:port seen by the robot, avoiding public NAT hairpin and mDNS host lookup.
    void start_local_stun_server();
    void stop_local_stun_server();
    void stun_server_loop(uint16_t port);

    // Publishers
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr offer_id_pub_;    // {client_id, sdp}
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr ice_out_id_pub_;  // {client_id, ...}
    rclcpp::Publisher<std_msgs::msg::String>::SharedPtr active_streams_pub_;

    // Subscribers (signaling only; the per-camera image subscriptions live in cameras_)
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr start_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr answer_id_sub_;
    rclcpp::Subscription<std_msgs::msg::String>::SharedPtr ice_in_id_sub_;

    // ---- Persistent encode pipeline (built once, never torn down until shutdown) ----
    GstElement* encode_pipeline_ = nullptr;
    std::vector<std::unique_ptr<CameraEncoder>> cameras_;  // configured cameras, in m-line order

    // ---- Shared audio (mic) pipeline: encoded once, fanned out to peers, gated for mic privacy ----
    GstElement* audio_pipeline_ = nullptr;   // built once if enable_audio_; alsasrc..rtpopuspay..appsink
    GstElement* audio_sink_ = nullptr;       // ref'd appsink (the fan-out tap)
    std::atomic<int> want_audio_{0};         // # peers with audio active; >0 => mic open (pipeline PLAYING)
    std::atomic<uint64_t> audio_frames_{0};  // for status: is audio actually flowing
    bool audio_playing_ = false;             // current audio pipeline state (gated by want_audio_)

    // ---- Peers ----
    std::map<std::string, std::unique_ptr<Peer>> peers_;
    std::mutex peers_mutex_;

    // Source mode (global across peers; last START wins, re-points the shared subscriptions).
    std::string current_source_ = "live";
    rclcpp::QoS camera_qos_;

    // Timers
    rclcpp::TimerBase::SharedPtr health_timer_;
    rclcpp::TimerBase::SharedPtr status_timer_;
    rclcpp::TimerBase::SharedPtr adapt_timer_;

    // Worst viewer-reported link quality from the last stats round (-1 = no report yet). Written by
    // webrtcbin stats callbacks, read by the 1 Hz adaptation tick.
    std::atomic<int> rtcp_loss_promille_{-1};
    std::atomic<int> rtcp_rtt_ms_{-1};
    std::atomic<bool> degraded_{false};  // adaptation state; also read on webrtcbin threads (on-new-transceiver)
    int adapt_bad_ticks_ = 0;
    int adapt_good_ticks_ = 0;

    // RTCP-inactivity watchdog timeout (seconds without RTCP before a peer's transport is released).
    double rtcp_inactivity_timeout_s_ = kDefaultRtcpInactivityTimeoutS;
    rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr rtcp_timeout_cb_;

    std::chrono::steady_clock::time_point prev_status_time_;

    // Config
    bool use_compressed_images_ = false;
    bool enable_audio_ = true;
    std::string audio_source_element_ = "alsasrc";
    std::string audio_capture_device_;
    guint playout_min_delay_ms_ = 0;
    guint playout_max_delay_ms_ = 40;
    bool video_nack_ = true;
    guint video_fec_percentage_ = 25;
    bool enable_local_stun_ = true;
    int local_stun_port_ = 3478;
    std::atomic<bool> stun_running_{false};
    std::thread stun_thread_;
};

}  // namespace mars_cam

#endif  // MARS_CAM__WEBRTC_STREAMER_HPP_
