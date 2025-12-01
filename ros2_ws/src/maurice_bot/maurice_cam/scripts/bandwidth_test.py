#!/usr/bin/env python3
"""
Camera Bandwidth Test
Compares raw vs compressed camera topic performance.
Measures: frequency (Hz), bandwidth (MB/s), message size, latency.
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from sensor_msgs.msg import Image, CompressedImage
from collections import deque
import time
import sys


class TopicStats:
    """Track statistics for a single topic."""
    
    def __init__(self, name: str, window_size: int = 100):
        self.name = name
        self.window_size = window_size
        self.timestamps = deque(maxlen=window_size)
        self.sizes = deque(maxlen=window_size)
        self.latencies = deque(maxlen=window_size)
        self.total_messages = 0
        self.total_bytes = 0
        self.start_time = None
    
    def record(self, msg_size: int, msg_stamp_sec: float):
        now = time.time()
        if self.start_time is None:
            self.start_time = now
        
        self.timestamps.append(now)
        self.sizes.append(msg_size)
        self.total_messages += 1
        self.total_bytes += msg_size
        
        # Calculate latency if message has valid timestamp
        if msg_stamp_sec > 0:
            latency_ms = (now - msg_stamp_sec) * 1000
            if latency_ms > 0 and latency_ms < 5000:  # Sanity check
                self.latencies.append(latency_ms)
    
    def get_frequency(self) -> float:
        """Calculate frequency in Hz from recent messages."""
        if len(self.timestamps) < 2:
            return 0.0
        dt = self.timestamps[-1] - self.timestamps[0]
        if dt <= 0:
            return 0.0
        return (len(self.timestamps) - 1) / dt
    
    def get_avg_size(self) -> float:
        """Average message size in bytes."""
        if not self.sizes:
            return 0.0
        return sum(self.sizes) / len(self.sizes)
    
    def get_bandwidth(self) -> float:
        """Bandwidth in MB/s."""
        freq = self.get_frequency()
        avg_size = self.get_avg_size()
        return (freq * avg_size) / (1024 * 1024)
    
    def get_avg_latency(self) -> float:
        """Average latency in ms."""
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)
    
    def get_total_bandwidth(self) -> float:
        """Total average bandwidth since start in MB/s."""
        if self.start_time is None:
            return 0.0
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return 0.0
        return (self.total_bytes / elapsed) / (1024 * 1024)


class BandwidthTestNode(Node):
    """Node to test camera topic bandwidth and frequency."""
    
    def __init__(self):
        super().__init__('camera_bandwidth_test')
        
        # Topics to test
        self.topics = {
            'raw_main': '/mars/main_camera/image',
            'compressed_main': '/mars/main_camera/image/compressed',
            'raw_stereo': '/mars/main_camera/stereo',
        }
        
        # Stats trackers
        self.stats = {name: TopicStats(name) for name in self.topics}
        
        # QoS for camera topics
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Create subscriptions
        self.subscriptions_list = []
        
        # Raw image subscriptions
        for name, topic in self.topics.items():
            if 'compressed' in name:
                sub = self.create_subscription(
                    CompressedImage,
                    topic,
                    lambda msg, n=name: self.compressed_callback(msg, n),
                    image_qos
                )
            else:
                sub = self.create_subscription(
                    Image,
                    topic,
                    lambda msg, n=name: self.raw_callback(msg, n),
                    image_qos
                )
            self.subscriptions_list.append(sub)
            self.get_logger().info(f"Subscribed to: {topic}")
        
        # Timer for printing stats
        self.create_timer(2.0, self.print_stats)
        
        self.get_logger().info("=" * 70)
        self.get_logger().info("Camera Bandwidth Test Started")
        self.get_logger().info("Comparing raw vs compressed camera topics")
        self.get_logger().info("=" * 70)
    
    def raw_callback(self, msg: Image, topic_name: str):
        """Handle raw image messages."""
        msg_size = len(msg.data)
        msg_stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        self.stats[topic_name].record(msg_size, msg_stamp)
    
    def compressed_callback(self, msg: CompressedImage, topic_name: str):
        """Handle compressed image messages."""
        msg_size = len(msg.data)
        msg_stamp = msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9
        self.stats[topic_name].record(msg_size, msg_stamp)
    
    def print_stats(self):
        """Print statistics for all topics."""
        print("\n" + "=" * 90)
        print(f"{'Topic':<25} {'Count':>8} {'Hz':>8} {'Size':>12} {'BW (MB/s)':>12} {'Latency':>12}")
        print("-" * 90)
        
        for name, topic in self.topics.items():
            stats = self.stats[name]
            freq = stats.get_frequency()
            avg_size = stats.get_avg_size()
            bandwidth = stats.get_bandwidth()
            latency = stats.get_avg_latency()
            
            # Format size
            if avg_size >= 1024 * 1024:
                size_str = f"{avg_size / (1024*1024):.2f} MB"
            elif avg_size >= 1024:
                size_str = f"{avg_size / 1024:.1f} KB"
            else:
                size_str = f"{avg_size:.0f} B"
            
            # Format latency
            latency_str = f"{latency:.1f} ms" if latency > 0 else "N/A"
            
            print(f"{name:<25} {stats.total_messages:>8} {freq:>7.1f}  {size_str:>12} {bandwidth:>11.2f}  {latency_str:>12}")
        
        print("-" * 90)
        
        # Comparison summary
        raw_stats = self.stats.get('raw_main')
        comp_stats = self.stats.get('compressed_main')
        
        if raw_stats and comp_stats and raw_stats.total_messages > 0 and comp_stats.total_messages > 0:
            raw_size = raw_stats.get_avg_size()
            comp_size = comp_stats.get_avg_size()
            
            if comp_size > 0:
                compression_ratio = raw_size / comp_size
                bandwidth_savings = (1 - comp_size / raw_size) * 100
                
                print(f"\nComparison (raw_main vs compressed_main):")
                print(f"  Compression ratio: {compression_ratio:.1f}x")
                print(f"  Bandwidth savings: {bandwidth_savings:.1f}%")
                
                raw_freq = raw_stats.get_frequency()
                comp_freq = comp_stats.get_frequency()
                if raw_freq > 0:
                    freq_diff = ((comp_freq - raw_freq) / raw_freq) * 100
                    print(f"  Frequency difference: {freq_diff:+.1f}% (raw: {raw_freq:.1f} Hz, compressed: {comp_freq:.1f} Hz)")
        
        print("=" * 90)


def main(args=None):
    rclpy.init(args=args)
    
    print("\n" + "=" * 70)
    print("CAMERA BANDWIDTH TEST")
    print("=" * 70)
    print("This test compares raw vs compressed camera topic performance.")
    print("Press Ctrl+C to stop and see final summary.")
    print("=" * 70 + "\n")
    
    node = BandwidthTestNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\n\nTest stopped by user.")
        
        # Print final summary
        print("\n" + "=" * 70)
        print("FINAL SUMMARY")
        print("=" * 70)
        
        for name, stats in node.stats.items():
            if stats.total_messages > 0:
                elapsed = time.time() - stats.start_time if stats.start_time else 0
                total_mb = stats.total_bytes / (1024 * 1024)
                avg_bw = stats.get_total_bandwidth()
                
                print(f"\n{name} ({node.topics[name]}):")
                print(f"  Total messages: {stats.total_messages}")
                print(f"  Total data: {total_mb:.2f} MB")
                print(f"  Duration: {elapsed:.1f} s")
                print(f"  Average bandwidth: {avg_bw:.2f} MB/s")
                print(f"  Average frequency: {stats.total_messages / elapsed:.1f} Hz" if elapsed > 0 else "")
                print(f"  Average latency: {stats.get_avg_latency():.1f} ms")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

