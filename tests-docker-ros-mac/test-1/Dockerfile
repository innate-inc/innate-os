#
# Dockerfile for Maurice Robot (ROS 2 Humble) with zsh + oh-my-zsh
#

# Start with a minimal ROS 2 Humble base
FROM ros:humble-ros-base

# Build argument to choose between simulation and hardware mode
# Usage: docker build --build-arg MODE=simulation .
#        docker build --build-arg MODE=hardware .
ARG MODE=simulation

# 1. Install base packages needed before apt-dependencies.txt
RUN apt-get update && apt-get install -y \
    curl \
    iputils-ping \
    gnupg \
    lsb-release \
    htop \
    btop

# 2. Add Innate packages repository (for ros-humble-innate-rws, etc.)
RUN curl -fsSL https://innate-inc.github.io/innate-packages/pubkey.gpg | \
        gpg --dearmor -o /usr/share/keyrings/innate-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/innate-archive-keyring.gpg] https://innate-inc.github.io/innate-packages/ $(lsb_release -cs) main" | \
        tee /etc/apt/sources.list.d/innate.list > /dev/null

# 3. Copy and install common apt dependencies
COPY ros2_ws/apt-dependencies.common.txt /tmp/apt-dependencies.common.txt
RUN apt-get update && \
    grep -v '^#' /tmp/apt-dependencies.common.txt | grep -v '^$' | xargs apt-get install -y && \
    rm /tmp/apt-dependencies.common.txt

# 4. Install hardware-specific dependencies if in hardware mode
COPY ros2_ws/apt-dependencies.hardware.txt /tmp/apt-dependencies.hardware.txt
RUN if [ "$MODE" = "hardware" ]; then \
        apt-get update && \
        grep -v '^#' /tmp/apt-dependencies.hardware.txt | grep -v '^$' | xargs apt-get install -y; \
    fi && \
    rm /tmp/apt-dependencies.hardware.txt

# 5. Install pip packages
RUN pip install --upgrade \
    websockets \
    pydantic \
    'numpy<2' \
    'opencv-python<4.10' \
    h5py \
    cartesia \
    torch \
    torchvision \
    einops \
    python-dotenv \
    websocket-client

# 5. Install oh-my-zsh (for root, since containers typically run as root unless changed)
#    The official install script tries to prompt, so we run it in a way that doesn't hang.
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" \
    || true

# 6. Make zsh the default shell for root inside the container
SHELL ["/usr/bin/zsh", "-c"]
RUN chsh -s /usr/bin/zsh root

# 7. (Optional) Change oh-my-zsh theme for root
#    RobbyRussell is default; here we switch to `agnoster`, or pick another if you like
RUN sed -i 's/ZSH_THEME="robbyrussell"/ZSH_THEME="agnoster"/' /root/.zshrc

# 7b. Pre-approve GitHub SSH host key to avoid prompts
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null

# 8. Copy your entire innate-os directory (including dds, ros2_ws, .git for version info)
COPY ros2_ws /root/innate-os/ros2_ws
COPY dds /root/innate-os/dds
COPY scripts /root/innate-os/scripts

# For version tracking, git needs to be present
COPY .git /root/innate-os/.git

# 9. Build your ROS 2 workspace.
#    We have to source the system setup.zsh for colcon to find ROS packages.
WORKDIR /root/innate-os/ros2_ws
RUN source /opt/ros/humble/setup.zsh && colcon build

#
# 10. Patch the root .zshrc to set up your environment the same way
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

# 11. When the container starts with no command, we just run zsh (login shell).
#    You'll drop into an interactive oh-my-zsh environment with everything set up.
WORKDIR /root/innate-os
CMD ["zsh", "-l"]

