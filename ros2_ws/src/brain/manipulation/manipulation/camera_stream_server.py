#!/usr/bin/env python3
"""
Camera Streaming Server for ELP Dual Lens Camera
Run this on the Jetson/robot to stream the camera over the network.

IMPORTANT: This uses the SAME resolution as the recording driver (1280x480)
to ensure calibration matches the recorded data.

Usage:
    python3 camera_stream_server.py
    
Then connect from your PC using the calibration client.

You will have to kill any processes using the camera (e.g. driver) before running this.
"""

import cv2
import socket
import pickle
import struct
import threading
import time

# ============== CONFIGURATION ==============
CAMERA_DEVICE = "/dev/video4"
# MUST MATCH the recording driver settings!
FRAME_WIDTH = 1280   # Stereo width (was 3840)
FRAME_HEIGHT = 480   # Stereo height (was 1080)
FPS = 30
HOST = "0.0.0.0"  # Listen on all interfaces
PORT = 9999
# ============================================


class CameraStreamServer:
    def __init__(self):
        self.cap = None
        self.server_socket = None
        self.running = False
        self.clients = []
        self.lock = threading.Lock()
        
    def setup_camera(self):
        """Initialize camera."""
        self.cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)
        
        if not self.cap.isOpened():
            raise RuntimeError(f"Failed to open camera: {CAMERA_DEVICE}")
        
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, FPS)
        
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"Camera initialized: {actual_w}x{actual_h}")
        print(f"Left camera will be: {actual_w//2}x{actual_h}")
        
    def handle_client(self, client_socket, addr):
        """Handle individual client connection."""
        print(f"Client connected: {addr}")
        
        try:
            while self.running:
                ret, frame = self.cap.read()
                if not ret:
                    continue
                
                # Encode frame as JPEG for efficient transfer
                _, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                data = pickle.dumps(encoded)
                
                # Send size first, then data
                message_size = struct.pack("L", len(data))
                
                try:
                    client_socket.sendall(message_size + data)
                except (BrokenPipeError, ConnectionResetError):
                    break
                    
                time.sleep(1/FPS)  # Throttle to target FPS
                
        except Exception as e:
            print(f"Client {addr} error: {e}")
        finally:
            print(f"Client disconnected: {addr}")
            client_socket.close()
            
    def start(self):
        """Start the streaming server."""
        self.setup_camera()
        
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((HOST, PORT))
        self.server_socket.listen(5)
        
        # Get actual IP address
        hostname = socket.gethostname()
        try:
            ip = socket.gethostbyname(hostname)
        except:
            ip = "unknown"
            
        print(f"\n{'='*60}")
        print(f" Camera Streaming Server Started")
        print(f"{'='*60}")
        print(f" Resolution: {FRAME_WIDTH}x{FRAME_HEIGHT} (stereo)")
        print(f" Per camera: {FRAME_WIDTH//2}x{FRAME_HEIGHT}")
        print(f" Hostname: {hostname}")
        print(f" Listening on: {HOST}:{PORT}")
        print(f" Connect from PC using: python3 camera_calibration_client.py {ip}")
        print(f"{'='*60}")
        print("\nPress Ctrl+C to stop\n")
        
        self.running = True
        
        try:
            while self.running:
                client_socket, addr = self.server_socket.accept()
                client_thread = threading.Thread(
                    target=self.handle_client, 
                    args=(client_socket, addr)
                )
                client_thread.daemon = True
                client_thread.start()
                
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            self.running = False
            self.cap.release()
            self.server_socket.close()


if __name__ == "__main__":
    server = CameraStreamServer()
    server.start()

