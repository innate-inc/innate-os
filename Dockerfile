#
# Dockerfile for Maurice Robot (ROS 2 Humble) with zsh + oh-my-zsh
#

# Start with a minimal ROS 2 Humble base
FROM ros:humble-ros-base

# 1. Install system packages
#    - zsh and git/curl so we can install oh-my-zsh
#    - python3-colcon-* for building ROS packages
#    - tmux for your discovery script
#    - Python libraries you need (numpy, serial, pygame)
#    - fastdds CLI tools
RUN apt-get update && apt-get install -y \
    zsh \
    git \
    curl \
    python3-colcon-common-extensions \
    tmux \
    python3-numpy \
    python3-serial \
    python3-pygame \
    ros-humble-fastrtps \
 && rm -rf /var/lib/apt/lists/*

# 2. Install oh-my-zsh (for root, since containers typically run as root unless changed)
#    The official install script tries to prompt, so we run it in a way that doesn't hang.
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 3. Make zsh the default shell for root inside the container
SHELL ["/usr/bin/zsh", "-c"]
RUN chsh -s /usr/bin/zsh root

# 4. (Optional) Change oh-my-zsh theme for root
#    RobbyRussell is default; here we switch to `agnoster`, or pick another if you like
RUN sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 5. Copy your entire maurice-prod directory (including dds, ros2_ws, etc.)
WORKDIR /root/maurice-prod
COPY . /root/maurice-prod

# 6. Build your ROS 2 workspace.
#    We have to source the system setup.zsh for colcon to find ROS packages.
WORKDIR /root/maurice-prod/ros2_ws
RUN source /opt/ros/humble/setup.zsh && colcon build

#
# 7. Patch the root .zshrc to set up your environment the same way
#    "setup_dds.zsh" does. This means:
#      - Source ROS 2
#      - Source your workspace
#      - Export environment variables for RMW + discovery
#      - Dynamically generate the super_client_configuration.xml
#
#    We inject shell code at the end of /root/.zshrc so that
#    whenever you start a new zsh inside the container (interactive or not),
#    these environment vars are set and the file is created.
#
#    If you prefer, you can copy a local .zshrc instead.
#
RUN echo '\
# ----- Maurice custom environment -----\n\
# Source the ROS 2 setup\n\
source /opt/ros/humble/setup.zsh\n\
\n\
# Source the workspace setup\n\
source /root/maurice-prod/ros2_ws/install/setup.zsh\n\
\n\
# Source the DDS setup\n\
source /root/maurice-prod/dds/setup_dds.zsh\n\
' >> /root/.zshrc

# 8. When the container starts with no command, we just run zsh (login shell).
#    You’ll drop into an interactive oh-my-zsh environment with everything set up.
CMD ["zsh", "-l"]
