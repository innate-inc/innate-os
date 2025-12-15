#!/usr/bin/env python3
"""
WebRTC Streamer with switchable video sources.

Supports:
- Live camera feeds (default)
- Replay feeds from recorder node

The source can be switched via the /webrtc/start message:
- Empty or {"source": "live"} -> use live camera topics
- {"source": "replay"} -> use replay topics from recorder
"""

import json
import numpy as np
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
from sensor_msgs.msg import Image, CompressedImage
from std_msgs.msg import String


class SimpleWebRTCStreamer(Node):
    def __init__(self):
        super().__init__('simple_webrtc_streamer')
        Gst.init(None)
        
        # Declare parameters for topic configuration
        self.declare_parameter('live_main_camera_topic', '/mars/main_camera/image/compressed')
        self.declare_parameter('live_arm_camera_topic', '/mars/arm/image_raw/compressed')
        self.declare_parameter('replay_main_camera_topic', '/brain/recorder/replay/main_camera/compressed')
        self.declare_parameter('replay_arm_camera_topic', '/brain/recorder/replay/arm_camera/compressed')
        
        # Get parameter values
        self.live_main_topic = self.get_parameter('live_main_camera_topic').value
        self.live_arm_topic = self.get_parameter('live_arm_camera_topic').value
        self.replay_main_topic = self.get_parameter('replay_main_camera_topic').value
        self.replay_arm_topic = self.get_parameter('replay_arm_camera_topic').value
        
        # Current source mode: "live" or "replay"
        self.current_source = "live"
        
        # Simple publishers and subscribers (default QoS)
        self.offer_pub = self.create_publisher(String, '/webrtc/offer', 10)
        self.ice_out_pub = self.create_publisher(String, '/webrtc/ice_out', 10)
        
        self.answer_sub = self.create_subscription(String, '/webrtc/answer', self.on_answer, 10)
        self.ice_in_sub = self.create_subscription(String, '/webrtc/ice_in', self.on_ice_in, 10)
        self.start_sub = self.create_subscription(String, '/webrtc/start', self.on_start, 10)
        
        # QoS for camera subscriptions
        self.camera_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Dynamic subscriptions (will be created/destroyed on source switch)
        self.image_sub_main = None
        self.image_sub_arm = None
        
        # Create initial subscriptions for live source
        self._create_subscriptions("live")
        
        # Single pipeline for single client
        self.pipe = None
        self.webrtc = None
        self.appsrc_main = None
        self.appsrc_arm = None
        
        self.get_logger().info(f'WebRTC Streamer ready (source: {self.current_source})')
        self.get_logger().info(f'  Live topics: {self.live_main_topic}, {self.live_arm_topic}')
        self.get_logger().info(f'  Replay topics: {self.replay_main_topic}, {self.replay_arm_topic}')
    
    def _destroy_subscriptions(self):
        """Destroy current image subscriptions"""
        if self.image_sub_main is not None:
            self.destroy_subscription(self.image_sub_main)
            self.image_sub_main = None
        if self.image_sub_arm is not None:
            self.destroy_subscription(self.image_sub_arm)
            self.image_sub_arm = None
    
    def _create_subscriptions(self, source: str):
        """Create image subscriptions for the given source"""
        self._destroy_subscriptions()
        
        if source == "replay":
            main_topic = self.replay_main_topic
            arm_topic = self.replay_arm_topic
        else:  # default to live
            main_topic = self.live_main_topic
            arm_topic = self.live_arm_topic
        
        self.image_sub_main = self.create_subscription(
            CompressedImage, main_topic, self.on_image_main, self.camera_qos
        )
        self.image_sub_arm = self.create_subscription(
            CompressedImage, arm_topic, self.on_image_arm, self.camera_qos
        )
        
        self.current_source = source
        self.get_logger().info(f'Subscribed to {source} sources: {main_topic}, {arm_topic}')
    
    def _process_image(self, msg, target_width=640, target_height=480):
        """Helper to process compressed image messages into RGB format"""
        import cv2
        
        # Decode compressed image
        np_arr = np.frombuffer(msg.data, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        
        if img is None:
            return None
            
        # Convert BGR to RGB (OpenCV uses BGR by default)
        img = img[:, :, ::-1]
        
        # Resize if needed
        if img.shape[0] != target_height or img.shape[1] != target_width:
            img = cv2.resize(img, (target_width, target_height))
        
        return img
    
    def on_image_main(self, msg):
        """Feed main camera images to pipeline"""
        if not self.appsrc_main:
            return
        
        img = self._process_image(msg, 640, 480)
        if img is None:
            return
        
        # Ensure contiguous memory layout
        img = np.ascontiguousarray(img)
        buf = Gst.Buffer.new_wrapped(img.tobytes())
        self.appsrc_main.emit('push-buffer', buf)
    
    def on_image_arm(self, msg):
        """Feed arm camera images to pipeline"""
        if not self.appsrc_arm:
            return
        
        img = self._process_image(msg, 640, 480)
        if img is None:
            return
        
        # Ensure contiguous memory layout
        img = np.ascontiguousarray(img)
        buf = Gst.Buffer.new_wrapped(img.tobytes())
        self.appsrc_arm.emit('push-buffer', buf)
    
    def cleanup_pipeline(self):
        """Clean up the pipeline and resources"""
        
        if self.pipe:
            self.get_logger().info('Cleaning up pipeline...')
            # Stop the pipeline
            self.pipe.set_state(Gst.State.NULL)
            self.pipe.get_state(Gst.CLOCK_TIME_NONE)
            # Clear references
            self.appsrc_main = None
            self.appsrc_arm = None
            self.webrtc = None
            self.pipe = None
            self.get_logger().info('Pipeline cleaned up')
    
    def on_start(self, msg):
        """Handle start request - cleanup old connection and create new one.
        
        Message format (JSON string):
        - Empty or {"source": "live"} -> use live camera topics
        - {"source": "replay"} -> use replay topics from recorder
        """
        # Parse source from message
        source = "live"  # default
        if msg.data.strip():
            try:
                data = json.loads(msg.data)
                source = data.get("source", "live")
            except json.JSONDecodeError:
                # Not JSON, treat as empty (use default)
                pass
        
        self.get_logger().info(f'START received (source={source}), creating offer...')
        
        # Switch subscriptions if source changed
        if source != self.current_source:
            self._create_subscriptions(source)
        
        # Always cleanup existing pipeline to handle reconnection
        self.cleanup_pipeline()
        
        # Create fresh pipeline with dual camera feeds via appsrc
        # Main camera on sink_0, arm camera on sink_1
        self.pipe = Gst.parse_launch(
            'webrtcbin name=webrtc '
            
            'appsrc name=src_main is-live=true format=time '
            'caps=video/x-raw,format=RGB,width=640,height=480,framerate=30/1 ! '
            'videoconvert ! '
            'vp8enc name=enc_main deadline=1 error-resilient=partitions keyframe-max-dist=30 ! '
            'rtpvp8pay name=pay_main pt=96 ! '
            'application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=96 ! '
            'webrtc.sink_0 '
            
            'appsrc name=src_arm is-live=true format=time '
            'caps=video/x-raw,format=RGB,width=640,height=480,framerate=15/1 ! '
            'videoconvert ! '
            'vp8enc deadline=1 error-resilient=partitions keyframe-max-dist=30 ! '
            'rtpvp8pay pt=97 ! '
            'application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=97 ! '
            'webrtc.sink_1'
        )
        
        # Get and configure main camera appsrc
        self.appsrc_main = self.pipe.get_by_name('src_main')
        self.appsrc_main.set_property('format', Gst.Format.TIME)
        self.appsrc_main.set_property('do-timestamp', True)
        self.appsrc_main.set_property('is-live', True)
        
        # Get and configure arm camera appsrc
        self.appsrc_arm = self.pipe.get_by_name('src_arm')
        self.appsrc_arm.set_property('format', Gst.Format.TIME)
        self.appsrc_arm.set_property('do-timestamp', True)
        self.appsrc_arm.set_property('is-live', True)
        
        self.webrtc = self.pipe.get_by_name('webrtc')
        
        # Enable BUNDLE for single ICE connection
        self.webrtc.set_property('bundle-policy', 3)  # max-bundle
        
        # Connect signals
        self.webrtc.connect('on-ice-candidate', lambda _, mline, cand: self.on_ice(mline, cand))
        self.webrtc.connect('notify::connection-state', lambda _, __: self.on_conn_state())
        self.webrtc.connect('notify::ice-connection-state', lambda obj, _: 
            self.get_logger().info(f'ICE state: {obj.get_property("ice-connection-state")}'))
        self.webrtc.connect('notify::ice-gathering-state', lambda obj, _: 
            self.get_logger().info(f'ICE gathering: {obj.get_property("ice-gathering-state")}'))
        
        # Start pipeline
        self.pipe.set_state(Gst.State.PLAYING)
        self.pipe.get_state(Gst.CLOCK_TIME_NONE)  # Wait for PLAYING
        
        self.get_logger().info('Pipeline PLAYING, creating offer...')
        
        # Create offer
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, None, None)
        self.webrtc.emit('create-offer', None, promise)
    
    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        
        self.get_logger().info(f'Offer created: {offer is not None}')
        
        # Set local description (don't wait)
        self.webrtc.emit('set-local-description', offer, Gst.Promise.new())
        
        # Send offer (raw SDP) with fixes for mobile compatibility
        msg = String()
        sdp_text = offer.sdp.as_text()
        
        # Add packetization-mode=1 which mobile devices require
        sdp_text = sdp_text.replace(
            'profile-level-id=42e01f',
            'profile-level-id=42e01f;packetization-mode=1;level-asymmetry-allowed=1'
        )
        
        # Add BUNDLE if not present (needed for single ICE connection)
        if 'a=group:BUNDLE' not in sdp_text:
            # Insert BUNDLE after the session-level attributes (after first m= line reference)
            lines = sdp_text.split('\r\n')
            for i, line in enumerate(lines):
                if line.startswith('t='):
                    lines.insert(i + 1, 'a=group:BUNDLE video0 video1')
                    break
            sdp_text = '\r\n'.join(lines)
        
        msg.data = sdp_text
        self.offer_pub.publish(msg)
        self.get_logger().info(f'Sent offer ({len(msg.data)} bytes)')
    
    def on_answer(self, msg):
        self.get_logger().info(f'Answer received ({len(msg.data)} bytes)')
        
        # Parse and set remote description
        ret, sdp = GstSdp.SDPMessage.new_from_text(msg.data)
        if ret != GstSdp.SDPResult.OK:
            self.get_logger().error(f'Failed to parse SDP answer: {ret}')
            return
            
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
        
        self.webrtc.emit('set-remote-description', answer, Gst.Promise.new())
        
        self.get_logger().info('Answer set')
    
    def on_ice_in(self, msg):
        ice = json.loads(msg.data)
        self.webrtc.emit('add-ice-candidate', ice['sdpMLineIndex'], ice['candidate'])
        self.get_logger().info(f'Added remote ICE: {ice["candidate"][:50]}...')
    
    def on_ice(self, mline, candidate):
        msg = String()
        msg.data = json.dumps({'candidate': candidate, 'sdpMLineIndex': mline})
        self.ice_out_pub.publish(msg)
        self.get_logger().info(f'Sent ICE {mline}: {candidate[:50]}...')
    
    def on_conn_state(self):
        state = self.webrtc.get_property('connection-state')
        self.get_logger().info(f'WebRTC connection state: {state.value_nick}')
    
    def on_ice_state(self):
        state = self.webrtc.get_property('ice-connection-state')
        self.get_logger().info(f'ICE connection state: {state.value_nick}')
    
    def on_bus_error(self, bus, msg):
        err, debug = msg.parse_error()
        self.get_logger().error(f'GStreamer error: {err.message}')
        self.get_logger().error(f'Debug info: {debug}')
    
    def on_bus_warning(self, bus, msg):
        warn, debug = msg.parse_warning()
        self.get_logger().warn(f'GStreamer warning: {warn.message}')
    
    def on_bus_state_changed(self, bus, msg):
        if msg.src == self.webrtc:
            old, new, pending = msg.parse_state_changed()
            self.get_logger().info(f'WebRTC element state: {old.value_nick} -> {new.value_nick}')


def main(args=None):
    rclpy.init(args=args)
    node = SimpleWebRTCStreamer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Clean up shared pipeline on shutdown
        node.cleanup_pipeline()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

