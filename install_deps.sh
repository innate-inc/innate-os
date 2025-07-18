#!/usr/bin/env bash
set -euo pipefail

#----------------------------------------
# 1. Install ROS 2 Humble Desktop
#----------------------------------------
echo "### 1. Installing ROS 2 Humble Desktop..."
sudo apt-get update
sudo apt-get install -y curl gnupg lsb-release

# Add ROS 2 apt repo
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc \
  | sudo apt-key add -
sudo sh -c 'echo "deb http://packages.ros.org/ros2/ubuntu \
  $(lsb_release -cs) main" \
  > /etc/apt/sources.list.d/ros2-latest.list'
sudo apt-get update

# Desktop install (core + RViz, demos, etc.)
sudo apt-get install -y ros-humble-desktop

# Source ROS for the rest of this script
source /opt/ros/humble/setup.bash

#----------------------------------------
# 2. Install system & build dependencies
#----------------------------------------
echo "### 2. Installing system & build dependencies..."
sudo apt-get install -y \
  build-essential \
  cmake \
  python3-colcon-common-extensions \
  python3-pip \
  python3-vcstool \
  python3-rosdep \
  python3-rosinstall-generator \
  # OpenCV C++ libs
  libopencv-dev \
  # HDF5
  libhdf5-dev \
  # GL / EGL (for mujoco & OpenGL support)
  libgl1-mesa-dev \
  libglu1-mesa-dev \
  libegl1-mesa-dev \
  libglew-dev

#----------------------------------------
# 3. Install extra ROS 2 packages via apt
#----------------------------------------
echo "### 3. Installing additional ROS 2 packages..."
sudo apt-get install -y \
  ros-humble-nav2-simple-commander \
  ros-humble-nav2-map-server \
  ros-humble-moveit-py \
  ros-humble-moveit-ros-move-group \
  ros-humble-kdl-parser-py \
  ros-humble-cv-bridge \
  ros-humble-camera-info-manager \
  ros-humble-depthai \
  ros-humble-depthai-bridge \
  ros-humble-depthai-examples \
  ros-humble-stage \
  ros-humble-rosbridge-server \
  ros-humble-rosbridge-suite \
  # Additional packages from Dockerfile
  ros-humble-fastrtps \
  ros-humble-launch-xml \
  ros-humble-navigation2 \
  ros-humble-demo-nodes-cpp \
  ros-humble-nav2-bringup \
  ros-humble-depthai-ros \
  ros-humble-rqt-plot \
  ros-humble-rviz2

#----------------------------------------
# 4. Install Python packages
#----------------------------------------
echo "### 4. Installing Python packages from requirements.txt..."
python3 -m pip install --upgrade pip setuptools
python3 -m pip install -r requirements.txt

echo "✅ All done! You have ROS 2 Humble Desktop plus all extra ROS modules, system libs, and Python deps."

