# Maurice Production OS

## WARNING

IF YOU EVER MAKE THIS PUBLIC, DELETE THE KEY FOR AXEL'S EMAILS sending in the primitive for send_email

## Simulation

The below is for running in dev mode, which is the only one that works right now.

Prod would be if we want to run the simulation in a container completely in an elastically scalable cloud environment. Right now we use a VM so dev is better.

First build the container:

```bash
docker compose -f docker-compose.dev.yml build
```

Then run the container:

```bash
docker compose -f docker-compose.dev.yml up -d
```

And then drop into the container:

```bash
docker compose -f docker-compose.dev.yml exec maurice zsh -l
```

You can use novnc to connect to rviz2. After launching rviz2 inside the container, you can connect to the instance in your browser:

```bash
http://localhost:8080/vnc.html
```

Inside the container, first run the discovery service:

```bash
discovery-and-launch-sim
```

VERIFY THAT THE IP ADDRESS IS CORRECT IN SETUP_DDS.ZSH

Then join the tmux session:

```bash
tmux a
```

Then run the simulation in a new tmux pane:

```bash
ros2 launch maurice_sim_bringup sim_rosbridge.launch.py
```

The run the nav system in a new tmux pane:

```bash
ros2 launch maurice_nav maurice_nav_launch.py
```

Then run the brain client in a new tmux pane:

```bash
ros2 launch brain_client brain_client.launch.py
```


## System setup

On the robot, make sure to execute the following to setup the system properly:

#### To allow the normal user to scan for wifis:

zsh
```
sudo mkdir -p /etc/polkit-1/localauthority/50-local.d/
sudo nano /etc/polkit-1/localauthority/50-local.d/10-networkmanager-wifi-scan.pkla
sudo systemctl restart polkit
```

And in the .pkla file:

```
[Allow WiFi Scanning]
Identity=unix-user:*
Action=org.freedesktop.NetworkManager.wifi.scan;org.freedesktop.NetworkManager.enable-disable-wifi;org.freedesktop.NetworkManager.settings.modify.system;org.freedesktop.NetworkManager.network-control
ResultAny=yes
ResultInactive=yes
ResultActive=yes
```

## Caveats

### On Ros2 version

For the action server to run properly you might need to update ros. This version below was shown to work well

```
dpkg -s ros-humble-ros-core | grep Version

Version: 0.10.0-1jammy.20250430.084858
```