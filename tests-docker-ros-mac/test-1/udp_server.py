#!/usr/bin/env python3

import socket

UDP_IP = "0.0.0.0"  # Listen on all interfaces on the host
UDP_PORT = 5005

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

print(f"UDP server listening on {UDP_IP}:{UDP_PORT}...")
while True:
    data, addr = sock.recvfrom(1024)  # buffer size is 1024 bytes
    print(f"Received message from {addr}: {data.decode()}")
