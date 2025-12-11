#!/usr/bin/env python3
"""
Lightweight TF Transform Plotter for Head Camera

Shows camera position in side view (X-Z) and pitch angle.

Usage:
    python3 tf_plotter.py
"""

import rclpy
from rclpy.node import Node
from tf2_ros import Buffer, TransformListener
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import numpy as np
from collections import deque
import math


class TFPlotter(Node):
    def __init__(self):
        super().__init__('tf_plotter')
        
        # TF2 buffer and listener
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        
        # Data storage for trail
        self.trail_length = 50
        self.x_data = deque(maxlen=self.trail_length)
        self.z_data = deque(maxlen=self.trail_length)
        self.pitch_data = deque(maxlen=200)
        self.time_data = deque(maxlen=200)
        
        # Current values
        self.current_x = 0.0
        self.current_z = 0.258
        self.current_pitch = 0.0
        self.start_time = None
        
        # Frame configuration
        self.parent_frame = 'base_link'
        self.child_frame = 'head_camera_link'
        
        # Setup plot
        self.setup_plot()
        
        # Timer to poll TF at 20 Hz
        self.timer = self.create_timer(1.0/20.0, self.update_data)
        
        self.get_logger().info(f'TF Plotter: {self.parent_frame} -> {self.child_frame}')
        
    def setup_plot(self):
        """Setup simple 2-panel figure."""
        plt.style.use('dark_background')
        
        self.fig, (self.ax_xz, self.ax_pitch) = plt.subplots(1, 2, figsize=(12, 5))
        self.fig.suptitle('Head Camera Transform', fontsize=14, fontweight='bold')
        
        # === Side View (X-Z) ===
        self.ax_xz.set_xlabel('X (forward) [mm]')
        self.ax_xz.set_ylabel('Z (height) [mm]')
        self.ax_xz.set_title('Side View (X-Z)')
        self.ax_xz.set_xlim(-20, 50)
        self.ax_xz.set_ylim(240, 280)
        self.ax_xz.set_aspect('equal')
        self.ax_xz.grid(True, alpha=0.3)
        
        # Reference lines
        self.ax_xz.axhline(y=258, color='gray', linestyle='--', alpha=0.4, linewidth=1)
        self.ax_xz.axvline(x=0, color='gray', linestyle='--', alpha=0.4, linewidth=1)
        
        # Trail and point
        self.trail_xz, = self.ax_xz.plot([], [], 'c-', alpha=0.4, linewidth=1)
        self.point_xz, = self.ax_xz.plot([], [], 'ro', markersize=14)
        
        # Direction arrow (will be recreated each frame)
        self.arrow = None
        
        # Position text
        self.pos_text = self.ax_xz.text(0.02, 0.98, '', transform=self.ax_xz.transAxes,
                                         fontsize=11, va='top', color='lime', family='monospace')
        
        # === Pitch Angle Plot ===
        self.ax_pitch.set_xlabel('Time (s)')
        self.ax_pitch.set_ylabel('Pitch (°)')
        self.ax_pitch.set_title('Head Pitch Angle')
        self.ax_pitch.set_ylim(-30, 20)
        self.ax_pitch.grid(True, alpha=0.3)
        
        # Limit lines
        self.ax_pitch.axhline(y=0, color='white', linestyle='-', alpha=0.3, linewidth=1)
        self.ax_pitch.axhline(y=-25, color='red', linestyle=':', alpha=0.5, linewidth=1)
        self.ax_pitch.axhline(y=15, color='red', linestyle=':', alpha=0.5, linewidth=1)
        self.ax_pitch.fill_between([-100, 100], -25, -30, color='red', alpha=0.1)
        self.ax_pitch.fill_between([-100, 100], 15, 20, color='red', alpha=0.1)
        
        self.line_pitch, = self.ax_pitch.plot([], [], 'lime', linewidth=2)
        
        # Current pitch text
        self.pitch_text = self.ax_pitch.text(0.98, 0.98, '', transform=self.ax_pitch.transAxes,
                                              fontsize=14, va='top', ha='right', 
                                              color='lime', family='monospace', fontweight='bold')
        
        plt.tight_layout()
        
    def quaternion_to_pitch(self, x, y, z, w):
        """Extract pitch from quaternion."""
        sinp = 2 * (w * y - z * x)
        if abs(sinp) >= 1:
            return math.copysign(math.pi / 2, sinp)
        return math.asin(sinp)
        
    def update_data(self):
        """Poll TF and update data."""
        try:
            transform = self.tf_buffer.lookup_transform(
                self.parent_frame, self.child_frame, rclpy.time.Time())
            
            self.current_x = transform.transform.translation.x
            self.current_z = transform.transform.translation.z
            
            qx = transform.transform.rotation.x
            qy = transform.transform.rotation.y
            qz = transform.transform.rotation.z
            qw = transform.transform.rotation.w
            self.current_pitch = self.quaternion_to_pitch(qx, qy, qz, qw)
            
            # Store trail data (convert to mm)
            self.x_data.append(self.current_x * 1000)
            self.z_data.append(self.current_z * 1000)
            
            # Store pitch history
            now = self.get_clock().now()
            if self.start_time is None:
                self.start_time = now
            elapsed = (now - self.start_time).nanoseconds / 1e9
            self.time_data.append(elapsed)
            self.pitch_data.append(math.degrees(self.current_pitch))
            
        except Exception:
            pass
    
    def animate(self, frame):
        """Update animation."""
        if len(self.x_data) < 1:
            return []
        
        x_mm = self.current_x * 1000
        z_mm = self.current_z * 1000
        pitch_deg = math.degrees(self.current_pitch)
        
        # Update side view (X-Z)
        self.trail_xz.set_data(list(self.x_data), list(self.z_data))
        self.point_xz.set_data([x_mm], [z_mm])
        
        # Update direction arrow showing camera viewing direction
        if self.arrow:
            self.arrow.remove()
        arrow_len = 20  # mm
        # Arrow points in camera forward direction (tilted by pitch)
        dx = arrow_len * math.cos(self.current_pitch)
        dz = arrow_len * math.sin(self.current_pitch)  # Positive pitch = looking up
        self.arrow = self.ax_xz.annotate('', 
            xy=(x_mm + dx, z_mm + dz), xytext=(x_mm, z_mm),
            arrowprops=dict(arrowstyle='->', color='lime', lw=2))
        
        # Update position text
        self.pos_text.set_text(f'X: {x_mm:+.1f} mm\nZ: {z_mm:.1f} mm')
        
        # Update pitch plot
        if len(self.time_data) > 1:
            self.line_pitch.set_data(list(self.time_data), list(self.pitch_data))
            t_max = max(self.time_data)
            self.ax_pitch.set_xlim(max(0, t_max - 10), t_max + 0.5)
        
        # Update pitch text
        direction = '↓' if pitch_deg < 0 else '↑'
        self.pitch_text.set_text(f'{pitch_deg:+.1f}° {direction}')
        
        return []
    
    def run(self):
        """Run the plotter."""
        self.ani = FuncAnimation(self.fig, self.animate, interval=100, blit=False, cache_frame_data=False)
        plt.show(block=False)
        
        try:
            while rclpy.ok():
                rclpy.spin_once(self, timeout_sec=0.02)
                plt.pause(0.02)
                if not plt.fignum_exists(self.fig.number):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            plt.close('all')


def main(args=None):
    rclpy.init(args=args)
    plotter = TFPlotter()
    try:
        plotter.run()
    except KeyboardInterrupt:
        pass
    finally:
        plotter.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
