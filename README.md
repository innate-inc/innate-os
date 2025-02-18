# Maurice Production OS

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
discovery
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


