#
# Dockerfile for Maurice Robot (ROS 2 Humble) with zsh + oh-my-zsh
#

# Start with a minimal ROS 2 Humble base
FROM ros:humble-ros-base

# 1. Install base packages needed before apt-dependencies.txt
RUN apt-get update && apt-get install -y \
    curl \
    iputils-ping

# 2. Copy and install all apt dependencies from the central list
COPY ros2_ws/apt-dependencies.txt /tmp/apt-dependencies.txt
RUN apt-get update && \
    grep -v '^#' /tmp/apt-dependencies.txt | grep -v '^$' | xargs apt-get install -y && \
    rm /tmp/apt-dependencies.txt

# 3. Install pip packages
RUN pip install --upgrade \
    websockets \
    pydantic \
    opencv-python \
    h5py \
    cartesia

# 4. Install oh-my-zsh (for root, since containers typically run as root unless changed)
#    The official install script tries to prompt, so we run it in a way that doesn't hang.
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 5. Make zsh the default shell for root inside the container
SHELL ["/usr/bin/zsh", "-c"]
RUN chsh -s /usr/bin/zsh root

# 6. (Optional) Change oh-my-zsh theme for root
#    RobbyRussell is default; here we switch to `agnoster`, or pick another if you like
RUN sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 7. Copy your entire innate-os directory (including dds, ros2_ws, .git for version info)
COPY ros2_ws /root/innate-os/ros2_ws
COPY dds /root/innate-os/dds
COPY .git /root/innate-os/.git
WORKDIR /root/innate-os

# 8. Build your ROS 2 workspace.
#    We have to source the system setup.zsh for colcon to find ROS packages.
WORKDIR /root/innate-os/ros2_ws
RUN source /opt/ros/humble/setup.zsh && colcon build

#
# 9. Patch the root .zshrc to set up your environment the same way
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
# ----- Innate OS custom environment -----\n\
# Source the ROS 2 setup\n\
source /opt/ros/humble/setup.zsh\n\
\n\
# Source the workspace setup\n\
source /root/innate-os/ros2_ws/install/setup.zsh\n\
\n\
# Source the DDS setup\n\
source /root/innate-os/dds/setup_dds.zsh\n\
\n\
# Set INNATE_OS_ROOT environment variable\n\
export INNATE_OS_ROOT=/root/innate-os\n\
' >> /root/.zshrc

# 10. When the container starts with no command, we just run zsh (login shell).
#    You'll drop into an interactive oh-my-zsh environment with everything set up.
CMD ["zsh", "-l"]
