# Maurice Production OS

## Simulation

First build the container:

```bash
docker build -t maurice-prod:latest .
```

Then run the container:

```bash
docker run --rm -it \
  --net=host \
  --name maurice_zsh \
  maurice-prod:latest
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
ros2 launch maurice_sim_bringup sim_bringup.launch.py
```
