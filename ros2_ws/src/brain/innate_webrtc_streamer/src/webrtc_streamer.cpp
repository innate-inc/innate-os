#include "innate_webrtc_streamer/webrtc_streamer.hpp"

#include <sstream>
#include <algorithm>

namespace innate_webrtc_streamer
{

WebRTCStreamer::WebRTCStreamer()
: Node("webrtc_streamer"),
  pipeline_(nullptr),
  webrtc_(nullptr),
  appsrc_main_(nullptr),
  appsrc_arm_(nullptr),
  current_source_("live"),
  camera_qos_(rclcpp::QoS(1).best_effort())
{
  // Initialize GStreamer
  gst_init(nullptr, nullptr);

  // Declare parameters
  this->declare_parameter("live_main_camera_topic", "/mars/main_camera/image/compressed");
  this->declare_parameter("live_arm_camera_topic", "/mars/arm/image_raw/compressed");
  this->declare_parameter("replay_main_camera_topic", "/brain/recorder/replay/main_camera/compressed");
  this->declare_parameter("replay_arm_camera_topic", "/brain/recorder/replay/arm_camera/compressed");

  // Get parameters
  live_main_topic_ = this->get_parameter("live_main_camera_topic").as_string();
  live_arm_topic_ = this->get_parameter("live_arm_camera_topic").as_string();
  replay_main_topic_ = this->get_parameter("replay_main_camera_topic").as_string();
  replay_arm_topic_ = this->get_parameter("replay_arm_camera_topic").as_string();

  // Create publishers
  offer_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/offer", 10);
  ice_out_pub_ = this->create_publisher<std_msgs::msg::String>("/webrtc/ice_out", 10);

  // Create subscribers
  answer_sub_ = this->create_subscription<std_msgs::msg::String>(
    "/webrtc/answer", 10,
    std::bind(&WebRTCStreamer::on_answer, this, std::placeholders::_1));
  
  ice_in_sub_ = this->create_subscription<std_msgs::msg::String>(
    "/webrtc/ice_in", 10,
    std::bind(&WebRTCStreamer::on_ice_in, this, std::placeholders::_1));
  
  start_sub_ = this->create_subscription<std_msgs::msg::String>(
    "/webrtc/start", 10,
    std::bind(&WebRTCStreamer::on_start, this, std::placeholders::_1));

  // Create initial subscriptions for live source
  create_subscriptions("live");

  RCLCPP_INFO(this->get_logger(), "WebRTC Streamer ready (source: %s)", current_source_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Live topics: %s, %s", live_main_topic_.c_str(), live_arm_topic_.c_str());
  RCLCPP_INFO(this->get_logger(), "  Replay topics: %s, %s", replay_main_topic_.c_str(), replay_arm_topic_.c_str());
}

WebRTCStreamer::~WebRTCStreamer()
{
  cleanup_pipeline();
}

void WebRTCStreamer::destroy_subscriptions()
{
  image_sub_main_.reset();
  image_sub_arm_.reset();
}

void WebRTCStreamer::create_subscriptions(const std::string& source)
{
  destroy_subscriptions();

  std::string main_topic, arm_topic;
  if (source == "replay") {
    main_topic = replay_main_topic_;
    arm_topic = replay_arm_topic_;
  } else {
    main_topic = live_main_topic_;
    arm_topic = live_arm_topic_;
  }

  image_sub_main_ = this->create_subscription<sensor_msgs::msg::CompressedImage>(
    main_topic, camera_qos_,
    std::bind(&WebRTCStreamer::on_image_main, this, std::placeholders::_1));

  image_sub_arm_ = this->create_subscription<sensor_msgs::msg::CompressedImage>(
    arm_topic, camera_qos_,
    std::bind(&WebRTCStreamer::on_image_arm, this, std::placeholders::_1));

  current_source_ = source;
  RCLCPP_INFO(this->get_logger(), "Subscribed to %s sources: %s, %s", 
              source.c_str(), main_topic.c_str(), arm_topic.c_str());
}

cv::Mat WebRTCStreamer::process_image(
  const sensor_msgs::msg::CompressedImage::SharedPtr& msg,
  int target_width, int target_height)
{
  // Decode compressed image
  cv::Mat img = cv::imdecode(cv::Mat(msg->data), cv::IMREAD_COLOR);
  if (img.empty()) {
    return cv::Mat();
  }

  // Convert BGR to RGB
  cv::cvtColor(img, img, cv::COLOR_BGR2RGB);

  // Resize if needed
  if (img.rows != target_height || img.cols != target_width) {
    cv::resize(img, img, cv::Size(target_width, target_height));
  }

  return img;
}

void WebRTCStreamer::push_frame(GstElement* appsrc, const cv::Mat& frame)
{
  if (!appsrc || frame.empty()) {
    return;
  }

  // Create GstBuffer from frame data
  size_t size = frame.total() * frame.elemSize();
  GstBuffer* buffer = gst_buffer_new_allocate(nullptr, size, nullptr);
  
  GstMapInfo map;
  gst_buffer_map(buffer, &map, GST_MAP_WRITE);
  memcpy(map.data, frame.data, size);
  gst_buffer_unmap(buffer, &map);

  // Push buffer to appsrc
  GstFlowReturn ret;
  g_signal_emit_by_name(appsrc, "push-buffer", buffer, &ret);
  gst_buffer_unref(buffer);
}

void WebRTCStreamer::on_image_main(const sensor_msgs::msg::CompressedImage::SharedPtr msg)
{
  std::lock_guard<std::mutex> lock(pipeline_mutex_);
  if (!appsrc_main_) {
    return;
  }

  cv::Mat img = process_image(msg, 640, 480);
  if (!img.empty()) {
    push_frame(appsrc_main_, img);
  }
}

void WebRTCStreamer::on_image_arm(const sensor_msgs::msg::CompressedImage::SharedPtr msg)
{
  std::lock_guard<std::mutex> lock(pipeline_mutex_);
  if (!appsrc_arm_) {
    return;
  }

  cv::Mat img = process_image(msg, 640, 480);
  if (!img.empty()) {
    push_frame(appsrc_arm_, img);
  }
}

void WebRTCStreamer::cleanup_pipeline()
{
  std::lock_guard<std::mutex> lock(pipeline_mutex_);
  
  if (pipeline_) {
    RCLCPP_INFO(this->get_logger(), "Cleaning up pipeline...");
    gst_element_set_state(pipeline_, GST_STATE_NULL);
    gst_object_unref(pipeline_);
    pipeline_ = nullptr;
    webrtc_ = nullptr;
    appsrc_main_ = nullptr;
    appsrc_arm_ = nullptr;
    RCLCPP_INFO(this->get_logger(), "Pipeline cleaned up");
  }
}

void WebRTCStreamer::on_start(const std_msgs::msg::String::SharedPtr msg)
{
  // Parse source from message
  std::string source = "live";
  if (!msg->data.empty()) {
    try {
      auto json = nlohmann::json::parse(msg->data);
      if (json.contains("source")) {
        source = json["source"].get<std::string>();
      }
    } catch (const nlohmann::json::exception&) {
      // Not JSON, use default
    }
  }

  RCLCPP_INFO(this->get_logger(), "START received (source=%s), creating offer...", source.c_str());

  // Switch subscriptions if source changed
  if (source != current_source_) {
    create_subscriptions(source);
  }

  // Cleanup existing pipeline
  cleanup_pipeline();

  std::lock_guard<std::mutex> lock(pipeline_mutex_);

  // Create pipeline
  const char* pipeline_str =
    "webrtcbin name=webrtc bundle-policy=max-bundle "
    
    "appsrc name=src_main is-live=true format=time "
    "caps=video/x-raw,format=RGB,width=640,height=480,framerate=30/1 ! "
    "videoconvert ! "
    "vp8enc deadline=1 error-resilient=partitions keyframe-max-dist=30 ! "
    "rtpvp8pay pt=96 ! "
    "application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=96 ! "
    "webrtc.sink_0 "
    
    "appsrc name=src_arm is-live=true format=time "
    "caps=video/x-raw,format=RGB,width=640,height=480,framerate=15/1 ! "
    "videoconvert ! "
    "vp8enc deadline=1 error-resilient=partitions keyframe-max-dist=30 ! "
    "rtpvp8pay pt=97 ! "
    "application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=97 ! "
    "webrtc.sink_1";

  GError* error = nullptr;
  pipeline_ = gst_parse_launch(pipeline_str, &error);
  if (error) {
    RCLCPP_ERROR(this->get_logger(), "Failed to create pipeline: %s", error->message);
    g_error_free(error);
    return;
  }

  // Get elements
  appsrc_main_ = gst_bin_get_by_name(GST_BIN(pipeline_), "src_main");
  appsrc_arm_ = gst_bin_get_by_name(GST_BIN(pipeline_), "src_arm");
  webrtc_ = gst_bin_get_by_name(GST_BIN(pipeline_), "webrtc");

  // Configure appsrc elements
  g_object_set(appsrc_main_, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "is-live", TRUE, nullptr);
  g_object_set(appsrc_arm_, "format", GST_FORMAT_TIME, "do-timestamp", TRUE, "is-live", TRUE, nullptr);

  // Connect signals
  g_signal_connect(webrtc_, "on-ice-candidate", G_CALLBACK(on_ice_candidate), this);
  g_signal_connect(webrtc_, "notify::connection-state", G_CALLBACK(on_connection_state_changed), this);
  g_signal_connect(webrtc_, "notify::ice-gathering-state", G_CALLBACK(on_ice_gathering_state_changed), this);

  // Start pipeline
  gst_element_set_state(pipeline_, GST_STATE_PLAYING);
  RCLCPP_INFO(this->get_logger(), "Pipeline PLAYING, creating offer...");

  // Create offer
  GstPromise* promise = gst_promise_new_with_change_func(on_offer_created, this, nullptr);
  g_signal_emit_by_name(webrtc_, "create-offer", nullptr, promise);
}

void WebRTCStreamer::on_offer_created(GstPromise* promise, gpointer user_data)
{
  auto* self = static_cast<WebRTCStreamer*>(user_data);

  gst_promise_wait(promise);
  const GstStructure* reply = gst_promise_get_reply(promise);
  
  GstWebRTCSessionDescription* offer = nullptr;
  gst_structure_get(reply, "offer", GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &offer, nullptr);

  if (!offer) {
    RCLCPP_ERROR(self->get_logger(), "Failed to create offer");
    gst_promise_unref(promise);
    return;
  }

  RCLCPP_INFO(self->get_logger(), "Offer created");

  // Set local description
  GstPromise* local_promise = gst_promise_new();
  g_signal_emit_by_name(self->webrtc_, "set-local-description", offer, local_promise);

  // Get SDP text
  gchar* sdp_text = gst_sdp_message_as_text(offer->sdp);
  std::string sdp_str(sdp_text);
  g_free(sdp_text);

  // Publish offer
  auto msg = std_msgs::msg::String();
  msg.data = sdp_str;
  self->offer_pub_->publish(msg);
  RCLCPP_INFO(self->get_logger(), "Sent offer (%zu bytes)", sdp_str.size());

  gst_webrtc_session_description_free(offer);
  gst_promise_unref(promise);
}

void WebRTCStreamer::on_answer(const std_msgs::msg::String::SharedPtr msg)
{
  std::lock_guard<std::mutex> lock(pipeline_mutex_);
  
  if (!webrtc_) {
    RCLCPP_WARN(this->get_logger(), "Received answer but no pipeline active");
    return;
  }

  RCLCPP_INFO(this->get_logger(), "Answer received (%zu bytes)", msg->data.size());

  // Parse SDP
  GstSDPMessage* sdp = nullptr;
  GstSDPResult ret = gst_sdp_message_new_from_text(msg->data.c_str(), &sdp);
  if (ret != GST_SDP_OK) {
    RCLCPP_ERROR(this->get_logger(), "Failed to parse SDP answer");
    return;
  }

  // Create answer description
  GstWebRTCSessionDescription* answer = 
    gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_ANSWER, sdp);

  // Set remote description
  GstPromise* promise = gst_promise_new();
  g_signal_emit_by_name(webrtc_, "set-remote-description", answer, promise);

  gst_webrtc_session_description_free(answer);
  RCLCPP_INFO(this->get_logger(), "Answer set");
}

void WebRTCStreamer::on_ice_in(const std_msgs::msg::String::SharedPtr msg)
{
  std::lock_guard<std::mutex> lock(pipeline_mutex_);
  
  if (!webrtc_) {
    return;
  }

  try {
    auto json = nlohmann::json::parse(msg->data);
    std::string candidate = json["candidate"].get<std::string>();
    int mline_index = json["sdpMLineIndex"].get<int>();

    g_signal_emit_by_name(webrtc_, "add-ice-candidate", mline_index, candidate.c_str());
    RCLCPP_INFO(this->get_logger(), "Added remote ICE: %.50s...", candidate.c_str());
  } catch (const nlohmann::json::exception& e) {
    RCLCPP_ERROR(this->get_logger(), "Failed to parse ICE candidate: %s", e.what());
  }
}

void WebRTCStreamer::on_ice_candidate(GstElement* /*webrtc*/, guint mline, gchar* candidate, gpointer user_data)
{
  auto* self = static_cast<WebRTCStreamer*>(user_data);

  nlohmann::json json;
  json["candidate"] = std::string(candidate);
  json["sdpMLineIndex"] = mline;

  auto msg = std_msgs::msg::String();
  msg.data = json.dump();
  self->ice_out_pub_->publish(msg);
  
  RCLCPP_INFO(self->get_logger(), "Sent ICE %u: %.50s...", mline, candidate);
}

void WebRTCStreamer::on_connection_state_changed(GstElement* webrtc, GParamSpec* /*pspec*/, gpointer user_data)
{
  auto* self = static_cast<WebRTCStreamer*>(user_data);
  
  GstWebRTCPeerConnectionState state;
  g_object_get(webrtc, "connection-state", &state, nullptr);

  const char* state_str = "unknown";
  switch (state) {
    case GST_WEBRTC_PEER_CONNECTION_STATE_NEW: state_str = "new"; break;
    case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTING: state_str = "connecting"; break;
    case GST_WEBRTC_PEER_CONNECTION_STATE_CONNECTED: state_str = "connected"; break;
    case GST_WEBRTC_PEER_CONNECTION_STATE_DISCONNECTED: state_str = "disconnected"; break;
    case GST_WEBRTC_PEER_CONNECTION_STATE_FAILED: state_str = "failed"; break;
    case GST_WEBRTC_PEER_CONNECTION_STATE_CLOSED: state_str = "closed"; break;
  }

  RCLCPP_INFO(self->get_logger(), "WebRTC connection state: %s", state_str);
}

void WebRTCStreamer::on_ice_gathering_state_changed(GstElement* webrtc, GParamSpec* /*pspec*/, gpointer user_data)
{
  auto* self = static_cast<WebRTCStreamer*>(user_data);
  
  GstWebRTCICEGatheringState state;
  g_object_get(webrtc, "ice-gathering-state", &state, nullptr);

  const char* state_str = "unknown";
  switch (state) {
    case GST_WEBRTC_ICE_GATHERING_STATE_NEW: state_str = "new"; break;
    case GST_WEBRTC_ICE_GATHERING_STATE_GATHERING: state_str = "gathering"; break;
    case GST_WEBRTC_ICE_GATHERING_STATE_COMPLETE: state_str = "complete"; break;
  }

  RCLCPP_INFO(self->get_logger(), "ICE gathering: %s", state_str);
}

}  // namespace innate_webrtc_streamer

int main(int argc, char* argv[])
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<innate_webrtc_streamer::WebRTCStreamer>();
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
