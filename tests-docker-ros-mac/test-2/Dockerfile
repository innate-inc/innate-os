FROM ros:humble
RUN apt-get update && apt-get install -y \
    ros-humble-rmw-fastrtps-cpp \
    ros-humble-demo-nodes-cpp \
    && rm -rf /var/lib/apt/lists/*
EXPOSE 11811/udp
CMD ["fastdds", "discovery", "-i", "0", "-l", "0.0.0.0", "-p", "11811"]
