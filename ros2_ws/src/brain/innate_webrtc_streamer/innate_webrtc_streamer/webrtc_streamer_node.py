#!/usr/bin/env python3
"""
WebRTC Streamer Node for low-latency H.264 video streaming.

This node subscribes to a ROS2 image topic and streams it via WebRTC to a client.
Signaling is done through ROS2 topics that can be accessed via rosbridge.
"""

import json
import threading
import time
from enum import Enum

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
from sensor_msgs.msg import Image
from std_msgs.msg import String
from cv_bridge import CvBridge
import numpy as np


class StreamState(Enum):
    """WebRTC streaming states"""
    IDLE = "IDLE"
    NEGOTIATING = "NEGOTIATING"
    STREAMING = "STREAMING"
    ERROR = "ERROR"


class WebRTCStreamerNode(Node):
    """
    ROS2 node that streams camera feed via WebRTC.
    
    Subscribes to:
        - /color/image (sensor_msgs/Image)
        - /webrtc/offer (std_msgs/String) - SDP offers from client
        - /webrtc/ice_in (std_msgs/String) - ICE candidates from client
    
    Publishes to:
        - /webrtc/answer (std_msgs/String) - SDP answers to client
        - /webrtc/ice_out (std_msgs/String) - ICE candidates to client
        - /webrtc/status (std_msgs/String) - Current streaming state
        - /webrtc/stats (std_msgs/String) - Streaming statistics (JSON)
    """
    
    def __init__(self):
        super().__init__('webrtc_streamer_node')
        
        # Initialize GStreamer
        Gst.init(None)
        
        # Declare parameters
        self.declare_parameter('video.width', 640)
        self.declare_parameter('video.height', 480)
        self.declare_parameter('video.fps', 30)
        self.declare_parameter('video.bitrate_kbps', 2500)
        
        self.declare_parameter('source.image_topic', '/color/image')
        
        self.declare_parameter('encoder.preset', 'superfast')
        self.declare_parameter('encoder.tune', 'zerolatency')
        self.declare_parameter('encoder.threads', 2)
        self.declare_parameter('encoder.keyint_frames', 30)
        self.declare_parameter('encoder.try_hardware', True)
        
        self.declare_parameter('network.stun_server', 'stun://stun.l.google.com:19302')
        
        self.declare_parameter('features.enable_stats', True)
        self.declare_parameter('features.stats_interval_sec', 5.0)
        self.declare_parameter('features.single_consumer', True)
        
        # Get parameters
        self.video_width = self.get_parameter('video.width').value
        self.video_height = self.get_parameter('video.height').value
        self.video_fps = self.get_parameter('video.fps').value
        self.video_bitrate_kbps = self.get_parameter('video.bitrate_kbps').value
        
        self.image_topic = self.get_parameter('source.image_topic').value
        
        self.encoder_preset = self.get_parameter('encoder.preset').value
        self.encoder_tune = self.get_parameter('encoder.tune').value
        self.encoder_threads = self.get_parameter('encoder.threads').value
        self.encoder_keyint = self.get_parameter('encoder.keyint_frames').value
        self.try_hardware = self.get_parameter('encoder.try_hardware').value
        
        self.stun_server = self.get_parameter('network.stun_server').value
        
        self.enable_stats = self.get_parameter('features.enable_stats').value
        self.stats_interval = self.get_parameter('features.stats_interval_sec').value
        self.single_consumer = self.get_parameter('features.single_consumer').value
        
        # State
        self.state = StreamState.IDLE
        self.pipeline = None
        self.webrtcbin = None
        self.appsrc = None
        self.bridge = CvBridge()
        self.last_image = None
        self.frame_count = 0
        self.stats_start_time = time.time()
        
        # Thread safety
        self.pipeline_lock = threading.Lock()
        
        # Detect encoder capabilities
        self.encoder_name, self.is_hardware = self._detect_encoder()
        self.get_logger().info(f'Using encoder: {self.encoder_name} (hardware={self.is_hardware})')
        
        # Create QoS profile for image subscription (best effort, like camera)
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1  # Only keep latest frame
        )
        
        # Create QoS profile for signaling (reliable)
        signaling_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10
        )
        
        # Subscribe to image topic
        self.image_sub = self.create_subscription(
            Image,
            self.image_topic,
            self.image_callback,
            image_qos
        )
        
        # Signaling subscribers
        self.offer_sub = self.create_subscription(
            String,
            '/webrtc/offer',
            self.on_offer_received,
            signaling_qos
        )
        
        self.ice_in_sub = self.create_subscription(
            String,
            '/webrtc/ice_in',
            self.on_ice_candidate_received,
            signaling_qos
        )
        
        # Signaling publishers
        self.answer_pub = self.create_publisher(String, '/webrtc/answer', signaling_qos)
        self.ice_out_pub = self.create_publisher(String, '/webrtc/ice_out', signaling_qos)
        self.status_pub = self.create_publisher(String, '/webrtc/status', signaling_qos)
        self.stats_pub = self.create_publisher(String, '/webrtc/stats', signaling_qos)
        
        # Stats timer
        if self.enable_stats:
            self.stats_timer = self.create_timer(
                self.stats_interval,
                self.publish_stats
            )
        
        # Publish initial status
        self.publish_status()
        
        self.get_logger().info(
            f'WebRTC Streamer initialized: {self.video_width}x{self.video_height} @ {self.video_fps}fps, '
            f'{self.video_bitrate_kbps}kbps'
        )
    
    def _detect_encoder(self):
        """Detect available H.264 encoder (hardware or software)"""
        if self.try_hardware:
            # Try hardware encoders in order of preference
            hw_encoders = [
                ('nvv4l2h264enc', 'NVIDIA V4L2 H.264 Encoder'),
                ('nvh264enc', 'NVIDIA H.264 Encoder'),
                ('omxh264enc', 'OpenMAX H.264 Encoder'),
                ('v4l2h264enc', 'V4L2 H.264 Encoder'),
            ]
            
            for encoder_name, description in hw_encoders:
                factory = Gst.ElementFactory.find(encoder_name)
                if factory is not None:
                    self.get_logger().info(f'Found hardware encoder: {description}')
                    return encoder_name, True
            
            self.get_logger().warn('No hardware H.264 encoder found, falling back to software')
        
        # Fall back to software encoder
        return 'x264enc', False
    
    def _create_pipeline(self):
        """Create GStreamer pipeline for WebRTC streaming"""
        with self.pipeline_lock:
            # Create pipeline
            self.pipeline = Gst.Pipeline.new('webrtc-pipeline')
            
            # Create appsrc for receiving ROS images
            self.appsrc = Gst.ElementFactory.make('appsrc', 'source')
            self.appsrc.set_property('caps', Gst.Caps.from_string(
                f'video/x-raw,format=RGB,width={self.video_width},height={self.video_height},'
                f'framerate={self.video_fps}/1'
            ))
            self.appsrc.set_property('format', Gst.Format.TIME)
            self.appsrc.set_property('is-live', True)
            self.appsrc.set_property('do-timestamp', True)
            
            # Create videoconvert for format conversion
            videoconvert = Gst.ElementFactory.make('videoconvert', 'convert')
            
            # Create encoder based on detected type
            if self.is_hardware:
                encoder = Gst.ElementFactory.make(self.encoder_name, 'encoder')
                # Hardware encoder settings
                if self.encoder_name == 'nvv4l2h264enc':
                    encoder.set_property('bitrate', self.video_bitrate_kbps * 1000)
                    encoder.set_property('insert-sps-pps', True)
                    encoder.set_property('idrinterval', self.encoder_keyint)
                elif self.encoder_name in ['nvh264enc', 'omxh264enc']:
                    encoder.set_property('bitrate', self.video_bitrate_kbps * 1000)
            else:
                # Software encoder (x264enc)
                encoder = Gst.ElementFactory.make('x264enc', 'encoder')
                encoder.set_property('speed-preset', self.encoder_preset)
                encoder.set_property('tune', self.encoder_tune)
                encoder.set_property('bitrate', self.video_bitrate_kbps)
                encoder.set_property('key-int-max', self.encoder_keyint)
                encoder.set_property('threads', self.encoder_threads)
                encoder.set_property('bframes', 0)  # No B-frames for low latency
            
            # Create h264parse
            h264parse = Gst.ElementFactory.make('h264parse', 'parse')
            
            # Create rtph264pay
            rtppay = Gst.ElementFactory.make('rtph264pay', 'pay')
            rtppay.set_property('config-interval', 1)  # Send SPS/PPS every second
            rtppay.set_property('pt', 96)
            
            # Create webrtcbin
            self.webrtcbin = Gst.ElementFactory.make('webrtcbin', 'webrtc')
            self.webrtcbin.set_property('bundle-policy', GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
            
            # Set STUN server if provided
            if self.stun_server:
                self.webrtcbin.set_property('stun-server', self.stun_server)
            
            # Add elements to pipeline
            self.pipeline.add(self.appsrc)
            self.pipeline.add(videoconvert)
            self.pipeline.add(encoder)
            self.pipeline.add(h264parse)
            self.pipeline.add(rtppay)
            self.pipeline.add(self.webrtcbin)
            
            # Link elements
            if not self.appsrc.link(videoconvert):
                raise RuntimeError('Failed to link appsrc to videoconvert')
            if not videoconvert.link(encoder):
                raise RuntimeError('Failed to link videoconvert to encoder')
            if not encoder.link(h264parse):
                raise RuntimeError('Failed to link encoder to h264parse')
            if not h264parse.link(rtppay):
                raise RuntimeError('Failed to link h264parse to rtppay')
            
            # Link rtppay to webrtcbin (via request pad)
            rtppay_src = rtppay.get_static_pad('src')
            webrtc_sink = self.webrtcbin.get_request_pad('sink_%u')
            if rtppay_src.link(webrtc_sink) != Gst.PadLinkReturn.OK:
                raise RuntimeError('Failed to link rtppay to webrtcbin')
            
            # Connect webrtcbin signals
            self.webrtcbin.connect('on-negotiation-needed', self.on_negotiation_needed)
            self.webrtcbin.connect('on-ice-candidate', self.on_ice_candidate)
            self.webrtcbin.connect('on-connection-state-change', self.on_connection_state_change)
            
            # Set up bus for error handling
            bus = self.pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect('message', self.on_bus_message)
            
            self.get_logger().info('GStreamer pipeline created successfully')
    
    def _destroy_pipeline(self):
        """Destroy the current pipeline"""
        with self.pipeline_lock:
            if self.pipeline:
                self.pipeline.set_state(Gst.State.NULL)
                self.pipeline = None
                self.webrtcbin = None
                self.appsrc = None
                self.get_logger().info('GStreamer pipeline destroyed')
    
    def image_callback(self, msg):
        """Callback for incoming ROS images"""
        try:
            # Convert ROS Image to numpy array
            if msg.encoding == 'rgb8':
                image_np = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
            elif msg.encoding == 'bgr8':
                # Convert BGR to RGB
                image_np = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
                image_np = image_np[:, :, ::-1]  # BGR to RGB
            else:
                self.get_logger().warn(f'Unsupported image encoding: {msg.encoding}')
                return
            
            # Resize if needed
            if image_np.shape[0] != self.video_height or image_np.shape[1] != self.video_width:
                import cv2
                image_np = cv2.resize(image_np, (self.video_width, self.video_height))
            
            self.last_image = image_np
            
            # Push to pipeline if streaming
            if self.state == StreamState.STREAMING and self.appsrc:
                self._push_frame(image_np)
                self.frame_count += 1
        
        except Exception as e:
            self.get_logger().error(f'Error in image_callback: {e}')
    
    def _push_frame(self, image_np):
        """Push a frame to the GStreamer pipeline"""
        try:
            # Create GStreamer buffer from numpy array
            data = image_np.tobytes()
            buf = Gst.Buffer.new_wrapped(data)
            
            # Push buffer to appsrc
            ret = self.appsrc.emit('push-buffer', buf)
            if ret != Gst.FlowReturn.OK:
                self.get_logger().warn(f'Error pushing buffer: {ret}')
        
        except Exception as e:
            self.get_logger().error(f'Error pushing frame: {e}')
    
    def on_offer_received(self, msg):
        """Handle SDP offer from client"""
        try:
            if self.single_consumer and self.state != StreamState.IDLE:
                self.get_logger().warn('Ignoring offer: stream already active (single consumer mode)')
                return
            
            self.get_logger().info('Received WebRTC offer')
            
            # Parse offer
            offer_sdp = msg.data
            
            # Create pipeline if needed
            if not self.pipeline:
                self._create_pipeline()
            
            # Start pipeline
            self.pipeline.set_state(Gst.State.PLAYING)
            self.state = StreamState.NEGOTIATING
            self.publish_status()
            
            # Set remote description
            ret, sdp_msg = GstSdp.SDPMessage.new_from_text(offer_sdp)
            if ret != GstSdp.SDPResult.OK:
                raise RuntimeError('Failed to parse SDP offer')
            
            offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdp_msg)
            promise = Gst.Promise.new_with_change_func(self.on_offer_set, None, None)
            self.webrtcbin.emit('set-remote-description', offer, promise)
        
        except Exception as e:
            self.get_logger().error(f'Error handling offer: {e}')
            self.set_error_state(f'SDP_PARSE: {e}')
    
    def on_offer_set(self, promise, user_data, user_data2):
        """Callback after remote description is set"""
        try:
            # Create answer
            promise = Gst.Promise.new_with_change_func(self.on_answer_created, None, None)
            self.webrtcbin.emit('create-answer', None, promise)
        
        except Exception as e:
            self.get_logger().error(f'Error in on_offer_set: {e}')
            self.set_error_state(f'ANSWER_CREATE: {e}')
    
    def on_answer_created(self, promise, user_data, user_data2):
        """Callback after answer is created"""
        try:
            promise.wait()
            reply = promise.get_reply()
            answer = reply.get_value('answer')
            
            # Set local description
            promise = Gst.Promise.new()
            self.webrtcbin.emit('set-local-description', answer, promise)
            promise.wait()
            
            # Send answer to client
            answer_sdp = answer.sdp.as_text()
            answer_msg = String()
            answer_msg.data = answer_sdp
            self.answer_pub.publish(answer_msg)
            
            self.get_logger().info('Sent WebRTC answer')
        
        except Exception as e:
            self.get_logger().error(f'Error creating answer: {e}')
            self.set_error_state(f'ANSWER_SEND: {e}')
    
    def on_ice_candidate(self, webrtcbin, mline_index, candidate):
        """Callback when local ICE candidate is found"""
        try:
            ice_msg = String()
            ice_msg.data = json.dumps({
                'candidate': candidate,
                'sdpMLineIndex': mline_index
            })
            self.ice_out_pub.publish(ice_msg)
            self.get_logger().debug(f'Sent ICE candidate: {candidate}')
        
        except Exception as e:
            self.get_logger().error(f'Error sending ICE candidate: {e}')
    
    def on_ice_candidate_received(self, msg):
        """Handle ICE candidate from client"""
        try:
            if not self.webrtcbin:
                self.get_logger().warn('Received ICE candidate but no webrtcbin exists')
                return
            
            ice_data = json.loads(msg.data)
            candidate = ice_data['candidate']
            mline_index = ice_data['sdpMLineIndex']
            
            self.webrtcbin.emit('add-ice-candidate', mline_index, candidate)
            self.get_logger().debug(f'Added ICE candidate: {candidate}')
        
        except Exception as e:
            self.get_logger().error(f'Error handling ICE candidate: {e}')
    
    def on_negotiation_needed(self, webrtcbin):
        """Callback when negotiation is needed"""
        self.get_logger().debug('Negotiation needed')
    
    def on_connection_state_change(self, webrtcbin, pspec):
        """Callback when connection state changes"""
        try:
            state = webrtcbin.get_property('connection-state')
            self.get_logger().info(f'WebRTC connection state: {state.value_nick}')
            
            if state == GstWebRTC.WebRTCPeerConnectionState.CONNECTED:
                self.state = StreamState.STREAMING
                self.publish_status()
                self.stats_start_time = time.time()
                self.frame_count = 0
            
            elif state == GstWebRTC.WebRTCPeerConnectionState.FAILED:
                self.get_logger().error('WebRTC connection failed')
                self.reset_to_idle()
            
            elif state == GstWebRTC.WebRTCPeerConnectionState.CLOSED:
                self.get_logger().info('WebRTC connection closed')
                self.reset_to_idle()
        
        except Exception as e:
            self.get_logger().error(f'Error in connection state change: {e}')
    
    def on_bus_message(self, bus, message):
        """Handle GStreamer bus messages"""
        t = message.type
        
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self.get_logger().error(f'GStreamer error: {err}, {debug}')
            self.set_error_state(f'PIPELINE: {err}')
            self.reset_to_idle()
        
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            self.get_logger().warn(f'GStreamer warning: {warn}, {debug}')
        
        elif t == Gst.MessageType.EOS:
            self.get_logger().info('GStreamer EOS received')
            self.reset_to_idle()
    
    def reset_to_idle(self):
        """Reset streaming to idle state"""
        self.get_logger().info('Resetting to IDLE state')
        self._destroy_pipeline()
        self.state = StreamState.IDLE
        self.frame_count = 0
        self.publish_status()
    
    def set_error_state(self, error_code):
        """Set error state with code"""
        self.state = StreamState.ERROR
        status_msg = String()
        status_msg.data = f'ERROR:{error_code}'
        self.status_pub.publish(status_msg)
    
    def publish_status(self):
        """Publish current status"""
        status_msg = String()
        status_msg.data = self.state.value
        self.status_pub.publish(status_msg)
    
    def publish_stats(self):
        """Publish streaming statistics"""
        if self.state != StreamState.STREAMING:
            return
        
        try:
            elapsed_time = time.time() - self.stats_start_time
            if elapsed_time > 0:
                fps = self.frame_count / elapsed_time
                
                stats = {
                    'state': self.state.value,
                    'fps': round(fps, 2),
                    'frames_sent': self.frame_count,
                    'elapsed_sec': round(elapsed_time, 2),
                    'encoder': self.encoder_name,
                    'hardware': self.is_hardware,
                    'resolution': f'{self.video_width}x{self.video_height}',
                    'target_fps': self.video_fps,
                    'bitrate_kbps': self.video_bitrate_kbps
                }
                
                stats_msg = String()
                stats_msg.data = json.dumps(stats)
                self.stats_pub.publish(stats_msg)
                
                self.get_logger().info(
                    f'WebRTC Stats: {fps:.1f} fps, {self.frame_count} frames, '
                    f'{elapsed_time:.1f}s, encoder={self.encoder_name}'
                )
        
        except Exception as e:
            self.get_logger().error(f'Error publishing stats: {e}')
    
    def destroy_node(self):
        """Clean up on node shutdown"""
        self.get_logger().info('Shutting down WebRTC streamer')
        self._destroy_pipeline()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = WebRTCStreamerNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info('Keyboard interrupt')
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()


