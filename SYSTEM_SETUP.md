# System Setup for Dynamic Networking and ROS/DDS Management

This document outlines the setup required to manage the Fast DDS discovery server and ROS 2 application using `systemd`, allowing for automatic restarts when the robot's IP address changes (e.g., due to Wi-Fi network changes triggered via the BLE provisioner service).

## Todo

Write a script that for a new robot installs everything where it needs to be

## Overview

The system uses `systemd` to manage three main services:

1.  **`discovery-server.service`**: Runs the `fastdds discovery` server.
2.  **`ros-app.service`**: Launches the main ROS 2 application within a `tmux` session using a wrapper script. This allows developers to attach to the `tmux` session for monitoring while still being managed by `systemd`.
3.  **`ble-provisioner.service`**: Runs the Python BLE service (`simple_bt_service.py`) which handles network changes.

When the BLE service connects the robot to a new network and detects an IP address change, it uses `sudo` to execute a helper script (`restart_robot_networking.sh`) which in turn uses `systemctl` to restart the `discovery-server` and `ros-app` services. The `setup_dds.zsh` script has been modified to dynamically detect the current IP, ensuring restarted services use the correct configuration.

## Setup Steps

**Important:** Replace placeholders like `jetson1`, `/home/jetson1`, `your_package_name`, and `your_launch_file.py` with your actual username, home directory, ROS package, and launch file names throughout these steps.

1.  **Update Scripts:**
    *   The script `innate-os/dds/setup_dds.zsh` has already been modified to dynamically detect the IP address.
    *   The script `innate-os/ros2_ws/src/maurice_bot/maurice_bt_provisioner/maurice_bt_provisioner/simple_bt_service.py` has been modified to detect IP changes and call the restart helper script.

2.  **Place Helper Scripts:**
    *   Copy the restart helper script to `/usr/local/bin`:
        ```bash
        sudo cp innate-os/scripts/restart_robot_networking.sh /usr/local/bin/
        sudo chmod +x /usr/local/bin/restart_robot_networking.sh
        ```
    *   Copy the tmux launcher script to `/usr/local/bin`:
        ```bash
        sudo cp innate-os/scripts/launch_ros_in_tmux.sh /usr/local/bin/
        sudo chmod +x /usr/local/bin/launch_ros_in_tmux.sh
        ```
    *   **Crucially, edit `/usr/local/bin/launch_ros_in_tmux.sh`** and update the `ROS_LAUNCH_PACKAGE` and `ROS_LAUNCH_FILE` variables to match your actual ROS application launch details.

3.  **Configure Sudoers:**
    *   Allow the user running the BLE service (`jetson1` in the examples) to run the restart script without a password.
    *   **Use `sudo visudo`** to edit the sudoers file. **Never edit it directly.**
    *   Add the following line at the end (replace `jetson1` if your user is different):
        ```
        jetson1 ALL=(ALL) NOPASSWD: /usr/local/bin/restart_robot_networking.sh
        ```
    *   Save and exit the editor.

4.  **Install Systemd Unit Files:**
    *   Copy the generated unit files to the systemd system directory:
        ```bash
        sudo cp innate-os/systemd/discovery-server.service /etc/systemd/system/
        sudo cp innate-os/systemd/ros-app.service /etc/systemd/system/
        sudo cp innate-os/systemd/ble-provisioner.service /etc/systemd/system/
        ```
    *   **Review the copied files in `/etc/systemd/system/`**: Ensure the `User`, `WorkingDirectory`, `ExecStart`, and script paths within the files are correct for your system setup (especially the `User` in `ros-app.service` and `ble-provisioner.service`).

5.  **Enable and Start Services:**
    *   Reload the systemd daemon to recognize the new files:
        ```bash
        sudo systemctl daemon-reload
        ```
    *   Enable the services to start automatically on boot:
        ```bash
        sudo systemctl enable discovery-server.service
        sudo systemctl enable ros-app.service
        sudo systemctl enable ble-provisioner.service
        ```
    *   Start the services manually for the first time (or reboot):
        ```bash
        sudo systemctl start discovery-server.service
        sudo systemctl start ble-provisioner.service
        sudo systemctl start ros-app.service 
        ```
        *Note: Starting `ros-app.service` last ensures its dependencies are likely met.*

## Usage and Monitoring

*   **Check Service Status:**
    ```bash
    sudo systemctl status discovery-server.service
    sudo systemctl status ros-app.service
    sudo systemctl status ble-provisioner.service
    ```
*   **View Logs:**
    ```bash
    sudo journalctl -u discovery-server.service -f
    sudo journalctl -u ros-app.service -f # Shows output from launch_ros_in_tmux.sh
    sudo journalctl -u ble-provisioner.service -f # Shows output from simple_bt_service.py
    ```
*   **Attach to ROS Tmux Session (for development):**
    *   Make sure you are logged in as the user specified in `ros-app.service` (`jetson1` in the example).
    *   Run:
        ```bash
        tmux attach -t ros_nodes 
        ```
    *   You can detach using `Ctrl+b` then `d`.
*   **Restarting Manually:**
    ```bash
    sudo systemctl restart ble-provisioner.service
    sudo systemctl restart discovery-server.service
    sudo systemctl restart ros-app.service
    ```
*   **Stopping:**
    ```bash
    sudo systemctl stop ros-app.service
    sudo systemctl stop ble-provisioner.service
    sudo systemctl stop discovery-server.service
    ```

## Troubleshooting

*   **Permission Denied (sudoers):** Ensure the `sudo visudo` line is correct and matches the user running `ble-provisioner.service`.
*   **Script Not Found:** Double-check the paths in the `.service` files (`ExecStart=`) and the path used in `simple_bt_service.py` (`RESTART_SCRIPT_PATH`). Verify the scripts in `/usr/local/bin` are executable (`chmod +x`).
*   **ROS Nodes Don't Start:** Check `journalctl -u ros-app.service`. Ensure environment sourcing works (`setup_dds.zsh`, ROS workspace `setup.zsh`). Verify the `ROS_LAUNCH_PACKAGE` and `ROS_LAUNCH_FILE` in `launch_ros_in_tmux.sh` are correct. Check permissions on the ROS workspace files.
*   **Tmux Session Issues:** Make sure `tmux` is installed. Check logs (`journalctl -u ros-app.service`). Try running `launch_ros_in_tmux.sh` manually as the correct user to debug.
*   **IP Address Not Updating:** Verify `hostname -I` gives the expected IP in `setup_dds.zsh`. Check `journalctl -u ble-provisioner.service` to see if the IP change is detected and the restart script is called. 
*   **Unable to read the topics / discovery server seemingly not working:** Make sure you import the .zshrc properly and if you zsh with oh-my-zsh, make sure you import the right .zshrc (pre-omz) like in the `zhrcs` folder. The .zshrc there imports the thing that has the right ros2 workspace stuff. TODO: Make a good .zshrc for everyone seriously. Not this janky stuff.