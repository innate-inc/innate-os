#!/usr/bin/env python3
"""
MINIMAL WebRTC Streamer - Barebone implementation for testing.
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
from sensor_msgs.msg import Image
from std_msgs.msg import String


class SimpleWebRTCStreamer(Node):
    def __init__(self):
        super().__init__('simple_webrtc_streamer')
        Gst.init(None)
        
        # Simple publishers and subscribers (default QoS)
        self.offer_pub = self.create_publisher(String, '/webrtc/offer', 10)
        self.ice_out_pub = self.create_publisher(String, '/webrtc/ice_out', 10)
        
        self.answer_sub = self.create_subscription(String, '/webrtc/answer', self.on_answer, 10)
        self.ice_in_sub = self.create_subscription(String, '/webrtc/ice_in', self.on_ice_in, 10)
        self.start_sub = self.create_subscription(String, '/webrtc/start', self.on_start, 10)
        
        # Subscribe to camera
        camera_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1
        )
        self.image_sub = self.create_subscription(Image, '/color/image', self.on_image, camera_qos)
        
        self.pipe = None
        self.webrtc = None
        self.appsrc = None
        
        self.get_logger().info('Simple WebRTC Streamer ready (camera mode)')
    
    def on_image(self, msg):
        # Feed camera images to pipeline
        if not self.appsrc:
            return
            
        if msg.encoding == 'bgr8':
            img = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
            img = img[:, :, ::-1]  # BGR to RGB
        elif msg.encoding == 'rgb8':
            img = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 3)
        else:
            return
        
        # Resize if needed
        if img.shape[0] != 480 or img.shape[1] != 640:
            import cv2
            img = cv2.resize(img, (640, 480))
        
        # Push frame - use try_push for non-blocking
        buf = Gst.Buffer.new_wrapped(img.tobytes())
        self.appsrc.emit('push-buffer', buf)
    
    def on_start(self, msg):
        self.get_logger().info('START received, creating offer...')
        
        # Create pipeline with camera feed via appsrc
        self.pipe = Gst.parse_launch(
            'appsrc name=src is-live=true format=time '
            'caps=video/x-raw,format=RGB,width=640,height=480,framerate=30/1 ! '
            'videoconvert ! '
            'vp8enc deadline=1 error-resilient=partitions keyframe-max-dist=30 ! '
            'rtpvp8pay pt=96 ! '
            'application/x-rtp,media=video,encoding-name=VP8,clock-rate=90000,payload=96 ! '
            'webrtcbin name=webrtc'
        )
        
        self.appsrc = self.pipe.get_by_name('src')
        self.appsrc.set_property('format', Gst.Format.TIME)
        self.appsrc.set_property('do-timestamp', True)
        self.appsrc.set_property('is-live', True)
        
        self.webrtc = self.pipe.get_by_name('webrtc')
        
        # Connect signals
        self.webrtc.connect('on-ice-candidate', lambda _, mline, cand: self.on_ice(mline, cand))
        self.webrtc.connect('notify::connection-state', lambda _, __: self.on_conn_state())
        
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
        
        # Send offer
        msg = String()
        msg.data = offer.sdp.as_text()
        self.offer_pub.publish(msg)
        
        self.get_logger().info(f'Sent offer ({len(msg.data)} bytes)')
    
    def on_answer(self, msg):
        self.get_logger().info(f'Answer received ({len(msg.data)} bytes)')
        
        # Parse and set remote description
        ret, sdp = GstSdp.SDPMessage.new_from_text(msg.data)
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
        self.get_logger().info(f'Connection state: {state.value_nick}')


def main(args=None):
    rclpy.init(args=args)
    node = SimpleWebRTCStreamer()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

