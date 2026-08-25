#include "mars_cam/webrtc_streamer.hpp"
#include "mars_cam/webrtc_internal.hpp"

#include <cv_bridge/cv_bridge.h>
#include <gst/app/app.h>
#include <gst/rtp/rtp.h>

#include <algorithm>
#include <cctype>  // std::isalnum (audio element/device validation)
#include <cmath>
#include <cstdlib>
#include <cstring>     // memcpy (push_frame)
#include <filesystem>  // /proc/asound (mic lookup)
#include <fstream>
#include <memory>
#include <sstream>

namespace mars_cam {

namespace {

// Backoff between attempts to reopen a mic that just refused to open.
constexpr int64_t kMicRetryBackoffNs = 10LL * 1000000000LL;

struct FanOutTarget {
    std::string client_id;
    std::string stream;
    GstElement* src = nullptr;
};

// A card can capture iff it exposes a pcm*c node.
bool card_can_capture(const std::filesystem::path& dir) {
    std::error_code ec;
    for (const auto& entry : std::filesystem::directory_iterator(dir, ec)) {
        const std::string node = entry.path().filename().string();
        if (node.rfind("pcm", 0) == 0 && node.back() == 'c') {
            return true;
        }
    }
    return false;
}

bool looks_like_camera(std::string name) {
    std::transform(name.begin(), name.end(), name.begin(), [](unsigned char c) { return std::tolower(c); });
    return name.find("camera") != std::string::npos || name.find("webcam") != std::string::npos;
}

// A dedicated mic beats a camera's built-in one, and both beat the Tegra I2S card, whose capture nodes are
// XBAR links with nothing on the far end. Mirrors the ladder workspace/inputs/micro_input.py walks, so the
// agent and the teleop stream land on the same microphone.
int capture_rank(const std::string& driver, const std::string& long_name) {
    if (driver != "USB-Audio") {
        return 1;
    }
    return looks_like_camera(long_name) ? 2 : 3;
}

// Parses lines of /proc/asound/cards: " 0 [Camera         ]: USB-Audio - Arducam USB Camera".
// Each card's second (continuation) line has no brackets and is skipped.
std::string best_capture_card() {
    std::ifstream cards("/proc/asound/cards");
    std::string line;
    std::string best;
    int best_rank = 0;
    while (std::getline(cards, line)) {
        const std::size_t open = line.find('[');
        const std::size_t close = line.find("]: ");
        const std::size_t dash = line.find(" - ", close == std::string::npos ? 0 : close);
        if (open == std::string::npos || close == std::string::npos || dash == std::string::npos) {
            continue;
        }
        std::string id = line.substr(open + 1, close - open - 1);
        id.erase(id.find_last_not_of(' ') + 1);
        const std::string driver = line.substr(close + 3, dash - close - 3);
        const std::string long_name = line.substr(dash + 3);
        std::error_code ec;
        const std::filesystem::path dir = "/proc/asound/" + id;
        if (!std::filesystem::exists(dir, ec) || !card_can_capture(dir)) {
            continue;
        }
        const int rank = capture_rank(driver, long_name);
        if (rank > best_rank) {
            best_rank = rank;
            best = id;
        }
    }
    return best;
}

// The mic's ALSA card name is not the same on every MARS build, so a configured name that is absent here
// would open nothing and the operator would hear silence with no clue why. Anything not in CARD= form (an
// explicit hw:0,0) is a deliberate choice and is left alone.
std::string resolve_capture_device(const std::string& configured) {
    const std::size_t card = configured.find("CARD=");
    if (card == std::string::npos) {
        return configured;
    }
    std::error_code ec;
    if (std::filesystem::exists("/proc/asound/" + configured.substr(card + 5), ec)) {
        return configured;
    }
    const std::string best = best_capture_card();
    return best.empty() ? configured : configured.substr(0, card + 5) + best;
}
}  // namespace

// =============================================================================
// Persistent encode pipeline
// =============================================================================

std::string WebRTCStreamer::video_encode_branch(const CameraEncoder& cam) const {
    // appsrc -> encoder -> payloader -> appsink. The appsink is the fan-out tap: every connected peer's
    // transport appsrc is fed from here, so each camera is encoded exactly once regardless of peer count.
    return "appsrc name=src_" + cam.name +
           " is-live=true format=time caps=video/x-raw,format=BGR,width=" + std::to_string(cam.width) +
           ",height=" + std::to_string(cam.height) + ",framerate=" + std::to_string(cam.fps) +
           "/1 ! "
           "queue leaky=downstream max-size-buffers=1 max-size-time=0 max-size-bytes=0 ! "
           "videoconvert ! "
           "vp8enc deadline=1 target-bitrate=2000000 cpu-used=4 error-resilient=partitions keyframe-max-dist=15 "
           "end-usage=cbr buffer-size=600 buffer-initial-size=400 buffer-optimal-size=500 ! "
           "rtpvp8pay name=pay_" +
           cam.name + " pt=" + std::to_string(cam.pt) + " ssrc=" + std::to_string(cam.ssrc) +
           " ! "
           "appsink name=sink_" +
           cam.name + " emit-signals=true sync=false async=false max-buffers=2 drop=true ";
}

void WebRTCStreamer::attach_playout_delay_extension(const std::string& cam) {
    // Add the playout-delay extension to the payloader so it writes the 3 bytes into every RTP packet.
    // The matching a=extmap is emitted per peer from the transport appsrc caps (set in
    // create_peer_transport), keyed by the same extmap id.
    const std::string payloader = "pay_" + cam;
    GstElement* pay = gst_bin_get_by_name(GST_BIN(encode_pipeline_), payloader.c_str());
    if (!pay) {
        RCLCPP_WARN(this->get_logger(), "Missing %s; playout-delay extension not applied", payloader.c_str());
        return;
    }
    GstRTPHeaderExtension* ext =
        make_playout_delay_ext(kPlayoutDelayExtId, playout_min_delay_ms_, playout_max_delay_ms_);
    g_signal_emit_by_name(pay, "add-extension", ext);  // transfer full: payloader owns ext now
    gst_object_unref(pay);
}

bool WebRTCStreamer::build_encode_pipeline() {
    std::string desc;
    for (const auto& cam : cameras_) {
        desc += video_encode_branch(*cam);
    }
    GError* error = nullptr;
    encode_pipeline_ = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create encode pipeline: %s", error->message);
        g_error_free(error);
        if (encode_pipeline_) {
            gst_object_unref(encode_pipeline_);
            encode_pipeline_ = nullptr;
        }
        return false;
    }

    for (auto& cam : cameras_) {
        cam->appsrc = gst_bin_get_by_name(GST_BIN(encode_pipeline_), ("src_" + cam->name).c_str());
        cam->sink = gst_bin_get_by_name(GST_BIN(encode_pipeline_), ("sink_" + cam->name).c_str());
        if (!cam->appsrc || !cam->sink) {
            RCLCPP_ERROR(this->get_logger(), "Encode pipeline missing elements for camera '%s'", cam->name.c_str());
            return false;
        }
        // do-timestamp=FALSE: push_frame stamps each buffer's PTS explicitly from a monotonic clock. Left to
        // do-timestamp, rtpvp8pay was emitting RTP header timestamps stuck at 0, which iOS/Safari WebRTC drops.
        g_object_set(cam->appsrc, "format", GST_FORMAT_TIME, "do-timestamp", FALSE, "is-live", TRUE, "block", FALSE,
                     "max-bytes", 2 * cam->width * cam->height * 3, nullptr);
        cam->pool = create_frame_pool(cam->width, cam->height, 3);
        attach_playout_delay_extension(cam->name);
        // user_data = the CameraEncoder* so the one static handler knows which camera fired.
        g_signal_connect(cam->sink, "new-sample", G_CALLBACK(on_sample), cam.get());
    }

    GstStateChangeReturn ret = gst_element_set_state(encode_pipeline_, GST_STATE_PLAYING);
    if (ret == GST_STATE_CHANGE_ASYNC) {
        GstState state = GST_STATE_VOID_PENDING;
        GstState pending = GST_STATE_VOID_PENDING;
        ret = gst_element_get_state(encode_pipeline_, &state, &pending, 3 * GST_SECOND);
        if (ret == GST_STATE_CHANGE_ASYNC) {
            RCLCPP_WARN(this->get_logger(), "Encode pipeline still changing state (state=%s, pending=%s)",
                        gst_element_state_get_name(state), gst_element_state_get_name(pending));
        }
    }
    if (ret == GST_STATE_CHANGE_FAILURE) {
        RCLCPP_ERROR(this->get_logger(), "Encode pipeline failed to reach PLAYING");
        return false;
    }
    std::string names;
    for (const auto& cam : cameras_)
        names += (names.empty() ? "" : "+") + cam->name;
    RCLCPP_INFO(this->get_logger(), "Persistent encode pipeline PLAYING (%s, idle until a peer connects)",
                names.c_str());
    return true;
}

void WebRTCStreamer::force_keyframe(const std::string& cam) {
    CameraEncoder* c = find_camera(cam);
    if (!c || !c->sink) {
        return;
    }
    GstElement* sink = c->sink;
    GstPad* sinkpad = gst_element_get_static_pad(sink, "sink");
    if (!sinkpad) {
        return;
    }
    // Upstream force-key-unit event (same structure GstVideo builds) so vp8enc emits an IDR a fresh or
    // just-resumed peer can decode immediately, rather than waiting up to keyframe-max-dist frames. Send
    // it on the peer (the payloader's src pad) so it travels upstream from there — sending an upstream
    // event straight at a sink pad warns about "wrong direction".
    GstPad* peer = gst_pad_get_peer(sinkpad);
    if (peer) {
        GstStructure* s = gst_structure_new("GstForceKeyUnit", "all-headers", G_TYPE_BOOLEAN, TRUE, "count",
                                            G_TYPE_UINT, static_cast<guint>(0), nullptr);
        gst_pad_send_event(peer, gst_event_new_custom(GST_EVENT_CUSTOM_UPSTREAM, s));
        gst_object_unref(peer);
    }
    gst_object_unref(sinkpad);
}

// =============================================================================
// Fan-out: encoded RTP -> every peer wanting this camera
// =============================================================================

GstFlowReturn WebRTCStreamer::on_sample(GstElement* appsink, gpointer user_data) {
    auto* cam = static_cast<CameraEncoder*>(user_data);  // wired in build_encode_pipeline
    cam->encoded_frames.fetch_add(1, std::memory_order_relaxed);
    cam->owner->fan_out_sample(appsink, cam->name);
    return GST_FLOW_OK;
}

// Shared fan-out: pull the encoded sample once, then under a BRIEF lock collect (and ref) the per-peer
// transport appsrcs that `select` returns, and copy+push to each OUTSIDE the lock. Holding peers_mutex_
// only for the lookup keeps fan-out at 30 fps x N peers from delaying the answer/ICE/health callbacks that
// contend on it; the refs keep an appsrc alive if its peer is torn down before the push (a push to a
// now-NULL appsrc just returns FLUSHING — harmless). A WRITABLE copy with PTS/DTS cleared is required, not
// a shared ref: the buffer carries the encode pipeline's (future-dated) PTS, and only a writable buffer
// lets the transport appsrc's do-timestamp re-stamp it to this pipeline's running-time — else webrtcbin
// holds every frame after the first burst.
void WebRTCStreamer::fan_out(GstElement* appsink, const std::function<GstElement*(Peer*)>& select,
                             const std::string& stream) {
    GstSample* sample = gst_app_sink_pull_sample(GST_APP_SINK(appsink));
    if (!sample) {
        return;
    }
    if (GstBuffer* buffer = gst_sample_get_buffer(sample)) {
        std::vector<FanOutTarget> targets;
        {
            std::lock_guard<std::mutex> lock(peers_mutex_);
            for (auto& kv : peers_) {
                if (GstElement* src = select(kv.second.get())) {
                    targets.push_back({kv.first, stream, GST_ELEMENT(gst_object_ref(src))});
                }
            }
        }
        for (auto& target : targets) {
            GstBuffer* out = gst_buffer_copy(buffer);
            GST_BUFFER_PTS(out) = GST_CLOCK_TIME_NONE;
            GST_BUFFER_DTS(out) = GST_CLOCK_TIME_NONE;
            GstFlowReturn ret;
            // The "push-buffer" action signal does NOT take ownership (transfer-none),
            // unlike gst_app_src_push_buffer(); the copy must be unreffed or it leaks
            // one RTP packet per push (~1 GB/h per viewing peer at 2 Mbps).
            g_signal_emit_by_name(target.src, "push-buffer", out, &ret);
            gst_buffer_unref(out);
            {
                std::lock_guard<std::mutex> lock(peers_mutex_);
                auto it = peers_.find(target.client_id);
                if (it != peers_.end()) {
                    Peer* p = it->second.get();
                    p->rtp_pushes[target.stream] += 1;
                    p->rtp_flow[target.stream] = gst_flow_get_name(ret);
                    if (ret != GST_FLOW_OK) {
                        p->rtp_push_errors[target.stream] += 1;
                    }
                }
            }
            gst_object_unref(target.src);
        }
    }
    gst_sample_unref(sample);
}

// Per-camera tap: fan this camera's encoded RTP to every peer actively pushing it.
void WebRTCStreamer::fan_out_sample(GstElement* appsink, const std::string& cam) {
    fan_out(
        appsink,
        [&cam](Peer* p) -> GstElement* {
            auto it = p->rtp.find(cam);
            return (p->media_ready->load(std::memory_order_relaxed) && it != p->rtp.end() && it->second &&
                    wants(p->active, cam))
                       ? it->second
                       : nullptr;
        },
        cam);
}

// =============================================================================
// Shared audio (mic): encoded once, fanned out, gated NULL/PLAYING for mic privacy
// =============================================================================

bool WebRTCStreamer::build_audio_pipeline() {
    // Validate the mic element + device (a plain element name / device string — these go into a parsed
    // pipeline description, so reject anything that could inject extra elements).
    const bool valid_element = !audio_source_element_.empty() &&
                               std::all_of(audio_source_element_.begin(), audio_source_element_.end(),
                                           [](unsigned char c) { return std::isalnum(c) || c == '-' || c == '_'; });
    if (!valid_element) {
        RCLCPP_ERROR(this->get_logger(), "audio_source_element '%s' is not a plain element name",
                     audio_source_element_.c_str());
        return false;
    }
    std::string src = audio_source_element_;
    if (!audio_capture_device_.empty()) {
        const bool valid_device =
            std::all_of(audio_capture_device_.begin(), audio_capture_device_.end(), [](unsigned char c) {
                return std::isalnum(c) || c == ':' || c == ',' || c == '.' || c == '=' || c == '-' || c == '_' ||
                       c == '/';
            });
        if (!valid_device) {
            RCLCPP_ERROR(this->get_logger(), "audio_capture_device '%s' has unexpected chars",
                         audio_capture_device_.c_str());
            return false;
        }
        const std::string resolved = resolve_capture_device(audio_capture_device_);
        if (resolved != audio_capture_device_) {
            RCLCPP_WARN(this->get_logger(), "audio_capture_device '%s' is not present; using '%s' instead",
                        audio_capture_device_.c_str(), resolved.c_str());
            audio_capture_device_ = resolved;
        }
        src += " device=\"" + audio_capture_device_ + "\"";
    }
    // mic -> opus -> rtp -> appsink (the fan-out tap). Encoded once for all peers; matches the RTP caps
    // each peer's transport audio appsrc declares (OPUS/48000/pt98).
    std::string desc = src +
                       " do-timestamp=true ! "
                       "queue leaky=downstream max-size-buffers=10 max-size-time=0 max-size-bytes=0 ! "
                       "audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
                       "opusenc bitrate=24000 audio-type=voice ! "
                       "rtpopuspay name=pay_audio pt=98 ! "
                       "appsink name=sink_audio emit-signals=true sync=false async=false max-buffers=4 drop=true ";
    GError* error = nullptr;
    audio_pipeline_ = gst_parse_launch(desc.c_str(), &error);
    if (error) {
        RCLCPP_ERROR(this->get_logger(), "Failed to create audio pipeline: %s", error->message);
        g_error_free(error);
        if (audio_pipeline_) {
            gst_object_unref(audio_pipeline_);
            audio_pipeline_ = nullptr;
        }
        return false;
    }
    audio_sink_ = gst_bin_get_by_name(GST_BIN(audio_pipeline_), "sink_audio");
    if (!audio_sink_) {
        RCLCPP_ERROR(this->get_logger(), "Audio pipeline missing appsink");
        return false;
    }
    g_signal_connect(audio_sink_, "new-sample", G_CALLBACK(on_audio_sample), this);
    // Stays NULL (mic closed) until a peer activates audio — reconcile_audio() opens it.
    RCLCPP_INFO(this->get_logger(), "Shared audio pipeline built (mic '%s', closed until a peer wants audio)",
                src.c_str());
    return true;
}

// Every mic failure lands here — a synchronous set_state refusal or an error the bus reports later —
// so the retry backoff cannot be sidestepped: an open that "succeeds" ASYNC and then fails on the bus
// would otherwise be reopened by every 5 Hz poll. Same locking contract as closing in reconcile_audio.
void WebRTCStreamer::fail_audio_pipeline() {
    gst_element_set_state(audio_pipeline_, GST_STATE_NULL);
    audio_playing_ = false;
    mic_retry_ns_ = std::chrono::steady_clock::now().time_since_epoch().count() + kMicRetryBackoffNs;
}

void WebRTCStreamer::reconcile_audio() {
    // Mic-privacy gate: the mic pipeline is PLAYING only while some peer has audio ACTIVE, NULL otherwise
    // (the device is genuinely closed). Opening (NULL->PLAYING) starts threads and never blocks, so it is
    // safe to call under peers_mutex_; CLOSING (->NULL) JOINS the fan-out streaming thread, which also
    // takes peers_mutex_ — so the close path must only run with the lock NOT held (it's driven by the
    // health poll). reconcile_audio() is therefore: callable under the lock when want_audio_>0 (opens),
    // and from poll_pipeline_health (outside the lock) for the general case (also closes).
    if (!audio_pipeline_) {
        return;
    }
    const bool want = want_audio_.load(std::memory_order_relaxed) > 0;
    if (want == audio_playing_) {
        return;
    }
    if (want) {
        // A mic that cannot open (unplugged, or held exclusively by another process) fails on every poll,
        // so back the retry off — at 5 Hz it buries the log under its own GStreamer errors for as long as
        // the operator leaves the toggle on.
        const int64_t now_ns = std::chrono::steady_clock::now().time_since_epoch().count();
        if (now_ns < mic_retry_ns_) {
            return;
        }
        if (gst_element_set_state(audio_pipeline_, GST_STATE_PLAYING) != GST_STATE_CHANGE_FAILURE) {
            audio_playing_ = true;
            mic_retry_ns_ = 0;
            RCLCPP_INFO(this->get_logger(), "Mic opened (a peer activated audio)");
        } else {
            fail_audio_pipeline();
            RCLCPP_WARN(this->get_logger(), "Mic '%s' failed to open; retrying in %lld s",
                        audio_capture_device_.empty() ? "(default)" : audio_capture_device_.c_str(),
                        static_cast<long long>(kMicRetryBackoffNs / 1000000000));
        }
    } else {
        gst_element_set_state(audio_pipeline_, GST_STATE_NULL);  // closes the device; joins the fan-out thread
        audio_playing_ = false;
        mic_retry_ns_ = 0;  // the next request tries immediately, whatever the last one cost
        RCLCPP_INFO(this->get_logger(), "Mic closed (no peer wants audio)");
    }
}

GstFlowReturn WebRTCStreamer::on_audio_sample(GstElement* appsink, gpointer user_data) {
    static_cast<WebRTCStreamer*>(user_data)->fan_out_audio(appsink);
    return GST_FLOW_OK;
}

// Audio tap: fan the shared mic's encoded RTP to every peer with audio active.
void WebRTCStreamer::fan_out_audio(GstElement* appsink) {
    audio_frames_.fetch_add(1, std::memory_order_relaxed);
    fan_out(
        appsink,
        [](Peer* p) -> GstElement* {
            auto it = p->rtp.find("audio");
            return (p->media_ready->load(std::memory_order_relaxed) && it != p->rtp.end() && it->second &&
                    p->audio_active)
                       ? it->second
                       : nullptr;
        },
        "audio");
}

// =============================================================================
// Camera subscriptions + frame ingest (gated by per-camera want-count)
// =============================================================================

void WebRTCStreamer::destroy_subscriptions() {
    for (auto& cam : cameras_) {
        cam->sub.reset();
    }
}

void WebRTCStreamer::set_camera_subscribed(CameraEncoder* cam, bool subscribed) {
    const bool have = static_cast<bool>(cam->sub);
    if (subscribed == have) {
        return;  // already in the desired state
    }
    if (!subscribed) {
        cam->sub.reset();
        RCLCPP_INFO(this->get_logger(), "Unsubscribed %s camera (no peer wants it)", cam->name.c_str());
        return;
    }
    const bool replay = current_source_ == "replay";
    const std::string topic = replay ? cam->replay_topic : cam->live_topic;
    if (use_compressed_images_) {
        cam->sub = this->create_subscription<sensor_msgs::msg::CompressedImage>(
            topic + "/compressed", camera_qos_,
            [this, cam](const sensor_msgs::msg::CompressedImage::SharedPtr msg) { on_image_compressed(cam, msg); });
    } else {
        cam->sub = this->create_subscription<sensor_msgs::msg::Image>(
            topic, camera_qos_, [this, cam](const sensor_msgs::msg::Image::SharedPtr msg) { on_image_raw(cam, msg); });
    }
    RCLCPP_INFO(this->get_logger(), "Subscribed %s camera: %s", cam->name.c_str(), topic.c_str());
}

void WebRTCStreamer::reconcile_subscriptions() {
    // A camera is worth receiving only if some connected peer negotiated it. Keying on negotiated (not
    // active) cameras keeps the subs alive for the whole session so a stream switch stays instant, while
    // an idle node (no peers) drops every camera sub and receives nothing. Caller holds peers_mutex_.
    for (auto& cam : cameras_) {
        bool need = false;
        for (auto& kv : peers_) {
            if (wants(kv.second->videos, cam->name)) {
                need = true;
                break;
            }
        }
        set_camera_subscribed(cam.get(), need);
    }
}

cv::Mat WebRTCStreamer::process_raw_image(const sensor_msgs::msg::Image::SharedPtr& msg, int target_width,
                                          int target_height) {
    if (!msg || msg->data.empty() || msg->height == 0 || msg->width == 0) {
        return cv::Mat();
    }
    // cv_bridge handles every supported encoding (rgb8/bgr8/mono8/rgba8/bgra8/…) -> BGR, sharing the
    // message buffer when it's already bgr8 and copying only when a conversion is genuinely needed.
    cv::Mat img;
    try {
        img = cv_bridge::toCvShare(msg, "bgr8")->image;
    } catch (const cv_bridge::Exception& e) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "cv_bridge (raw): %s", e.what());
        return cv::Mat();
    }
    if (img.rows == target_height && img.cols == target_width) {
        return img;  // shared with msg; stays valid until push_frame copies it into the GstBuffer
    }
    cv::Mat resized;
    cv::resize(img, resized, cv::Size(target_width, target_height));
    return resized;
}

cv::Mat WebRTCStreamer::process_compressed_image(const sensor_msgs::msg::CompressedImage::SharedPtr& msg,
                                                 int target_width, int target_height) {
    if (!msg || msg->data.empty()) {
        return cv::Mat();
    }
    cv::Mat img;
    try {
        img = cv_bridge::toCvCopy(msg, "bgr8")->image;  // decodes (jpeg/png) + converts to BGR
    } catch (const cv_bridge::Exception& e) {
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000, "cv_bridge (compressed): %s", e.what());
        return cv::Mat();
    }
    if (img.rows == target_height && img.cols == target_width) {
        return img;
    }
    cv::Mat resized;
    cv::resize(img, resized, cv::Size(target_width, target_height));
    return resized;
}

GstBufferPool* WebRTCStreamer::create_frame_pool(int width, int height, int channels) {
    gsize frame_size = width * height * channels;
    GstBufferPool* pool = gst_buffer_pool_new();
    GstStructure* config = gst_buffer_pool_get_config(pool);
    gst_buffer_pool_config_set_params(config, nullptr, frame_size, 2, 4);
    if (!gst_buffer_pool_set_config(pool, config)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to configure buffer pool");
        gst_object_unref(pool);
        return nullptr;
    }
    if (!gst_buffer_pool_set_active(pool, TRUE)) {
        RCLCPP_ERROR(this->get_logger(), "Failed to activate buffer pool");
        gst_object_unref(pool);
        return nullptr;
    }
    return pool;
}

void WebRTCStreamer::push_frame(CameraEncoder* cam, const cv::Mat& frame, const rclcpp::Time& stamp) {
    if (!cam->appsrc || frame.empty() || !cam->pool) {
        return;
    }
    GstBuffer* buffer = nullptr;
    if (gst_buffer_pool_acquire_buffer(cam->pool, &buffer, nullptr) != GST_FLOW_OK || !buffer) {
        return;
    }
    GstMapInfo map;
    gst_buffer_map(buffer, &map, GST_MAP_WRITE);
    memcpy(map.data, frame.data, frame.total() * frame.elemSize());
    gst_buffer_unmap(buffer, &map);

    // PTS from the frame's capture stamp, so inter-frame timing reflects true capture cadence. rtpvp8pay
    // turns this into the 90kHz RTP timestamp; a non-advancing one makes iOS/Safari drop the stream. The
    // stamp is untrusted: anchor on the first frame and re-anchor on any backward step (NTP correction,
    // replay loop) so PTS stays monotonic, and synthesize an fps-paced tick for an unstamped frame.
    const int64_t stamp_ns = stamp.nanoseconds();
    const GstClockTime tick = GST_SECOND / static_cast<GstClockTime>(std::max(cam->fps, 1));  // nominal frame step
    GstClockTime pts;
    if (stamp_ns > 0) {
        const GstClockTime s = static_cast<GstClockTime>(stamp_ns);
        if (cam->pts_base_ns == GST_CLOCK_TIME_NONE || s < cam->last_stamp_ns) {
            // Anchor one tick PAST the last PTS, not on it: s - (last_pts + tick) makes this frame's PTS
            // resume at last_pts + tick, so a re-anchor never repeats the previous frame's RTP timestamp.
            cam->pts_base_ns = s - (cam->last_pts_ns + tick);
        }
        cam->last_stamp_ns = s;
        pts = s - cam->pts_base_ns;
    } else {
        pts = cam->last_pts_ns + tick;
    }
    cam->last_pts_ns = pts;
    GST_BUFFER_PTS(buffer) = pts;
    GST_BUFFER_DTS(buffer) = pts;

    GstFlowReturn ret;
    g_signal_emit_by_name(cam->appsrc, "push-buffer", buffer, &ret);
    gst_buffer_unref(buffer);  // returns to pool
    cam->input_flow_code.store(static_cast<int>(ret), std::memory_order_relaxed);
    if (ret == GST_FLOW_OK) {
        cam->input_frames.fetch_add(1, std::memory_order_relaxed);
    } else {
        cam->input_push_errors.fetch_add(1, std::memory_order_relaxed);
        RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "Encode appsrc push for '%s' returned %s",
                             cam->name.c_str(), gst_flow_get_name(ret));
    }
}

void WebRTCStreamer::on_image_raw(CameraEncoder* cam, const sensor_msgs::msg::Image::SharedPtr& msg) {
    if (cam->want.load(std::memory_order_relaxed) == 0) {
        return;  // no peer wants this camera -> skip all encode work (flat memory, zero idle CPU)
    }
    cv::Mat img = process_raw_image(msg, cam->width, cam->height);
    if (!img.empty()) {
        push_frame(cam, img, rclcpp::Time(msg->header.stamp));
    }
}

void WebRTCStreamer::on_image_compressed(CameraEncoder* cam, const sensor_msgs::msg::CompressedImage::SharedPtr& msg) {
    if (cam->want.load(std::memory_order_relaxed) == 0) {
        return;
    }
    cv::Mat img = process_compressed_image(msg, cam->width, cam->height);
    if (!img.empty()) {
        push_frame(cam, img, rclcpp::Time(msg->header.stamp));
    }
}

}  // namespace mars_cam
