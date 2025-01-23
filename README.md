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
