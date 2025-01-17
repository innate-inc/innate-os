#!/usr/bin/env python3

import socket

# If you're on Docker Desktop for macOS or Windows, you can often use 'host.docker.internal'
# as the host address. If on Linux or custom networks, you might need the host's IP address.
UDP_IP = "0.0.0.0"
UDP_PORT = 5005
MESSAGE = "Hello from the container!"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(MESSAGE.encode(), (UDP_IP, UDP_PORT))
print(f"Sent message to {UDP_IP}:{UDP_PORT}")
