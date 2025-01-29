# Maurice Production OS

## Simulation

First build the container:

```bash
docker compose build
```

Then run the container:

```bash
docker compose up -d
```

And then drop into the container:

```bash
docker compose exec maurice zsh -l
```

Inside the container, first run the discovery service:

```bash
discovery
```

Then join the tmux session:

```bash
tmux a
```

Then run the simulation in a new tmux pane:

```bash
ros2 launch maurice_sim_bringup sim_rosbridge.launch.py
```

Then run the brain client in a new tmux pane:

```bash
ros2 launch brain_client brain_client.launch.py
```

In a 3rd tmux pane, run the tf publisher we need:

```bash
ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 map odom
```

In the 4th tmux pane, publish the URDF.

```bash
ros2 run robot_state_publisher robot_state_publisher turtlebot3_burger.urdf 
```

In the 5th tmux pane, run the odom tf broadcaster:

```bash
python3 /root/maurice-prod/ros2_ws/install/maurice_sim_bringup/lib/maurice_sim_bringup/odom_tf_broadcaster.py 
```

In the 6th tmux pane, run the nav2 node:

```bash
ros2 launch nav2_bringup navigation_launch.py use_sim_time:=false autostart:=true
```

