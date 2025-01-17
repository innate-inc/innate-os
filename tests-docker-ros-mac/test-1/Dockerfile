# Dockerfile
FROM ros:humble

# Install optional packages (e.g., pip)
RUN apt-get update && apt-get install -y --no-install-recommends \
  python3-pip \
  ros-humble-rmw-cyclonedds-cpp \
  ros-humble-demo-nodes-py \
  ros-humble-demo-nodes-cpp \
  iputils-ping \
  net-tools \
  iproute2 \
  && rm -rf /var/lib/apt/lists/*

# Copy in the container's CycloneDDS config
WORKDIR /root
COPY cyclonedds_container.xml /root/
COPY udp_client.py /root/
COPY udp_server.py /root/
# We'll drop into a shell on startup
CMD ["/bin/bash"]
