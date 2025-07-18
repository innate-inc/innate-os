#!/usr/bin/env bash
set -euo pipefail

# 1. Install ROS 2 Humble
echo "### 1. Installing ROS 2 Humble..."
sudo apt-get update
sudo apt-get install -y curl gnupg lsb-release
sudo curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc \
  | sudo apt-key add -
sudo sh -c 'echo "deb http://packages.ros.org/ros2/ubuntu \
  $(lsb_release -cs) main" > /etc/apt/sources.list.d/ros2-latest.list'
sudo apt-get update
sudo apt-get install -y ros-humble-desktop

# Source ROS 2 (for this script run only)
source /opt/ros/humble/setup.bash

# 2. Install system / build dependencies via apt
echo "### 2. Installing system & build dependencies..."
sudo apt-get install -y \
  build-essential \
  cmake \
  python3-colcon-common-extensions \
  python3-rosdep \
  python3-vcstool \
  python3-rosinstall-generator \
  python3-pip \
  # OpenCV C++ libs
  libopencv-dev \
  # HDF5
  libhdf5-dev \
  # GL / EGL (for mujoco, OpenGL support)
  libgl1-mesa-dev \
  libglu1-mesa-dev \
  libegl1-mesa-dev \
  libglew-dev

# Initialize rosdep (if not done already)
if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then
  sudo rosdep init
fi
rosdep update

# 3. Install Python packages
echo "### 3. Installing Python packages from requirements.txt..."
python3 -m pip install --upgrade pip setuptools
python3 -m pip install -r requirements.txt

echo "✅ All done! ROS 2 Humble, system libs, and Python deps are installed."
