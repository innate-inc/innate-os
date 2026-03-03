# scripts/rmw — ROS Middleware (Zenoh) Scripts

Quick reference for the DDS/Zenoh setup scripts. Audience: devs and agents who know ROS/Zenoh.

---

## `setup_dds.zsh`

**Source this in every terminal before running any ROS command** (or it's sourced automatically via `.zshrc`).

Sets:
- `RMW_IMPLEMENTATION=rmw_zenoh_cpp` — use Zenoh as the RMW layer
- `ROS_DOMAIN_ID=0`
- `ZENOH_ROUTER_CHECK_ATTEMPTS=0` — wait indefinitely for the router to start (don't bail early)
- Shared memory transport with a **12 MiB pool per node** (`transport/shared_memory/enabled=true`)
- Drop-wait tuning for congestion control on both session and router configs

```zsh
source ~/innate-os/scripts/rmw/setup_dds.zsh
```

Note: `ZENOH_CONFIG_OVERRIDE` is set to the session config (not router config) — the router script overrides this for itself.

---

## `start_zenoh_router.zsh`

**Managed by `systemd/zenoh-router.service`** — runs on startup. Don't call this manually unless debugging.

### Default mode (no args)

```zsh
./start_zenoh_router.zsh
```

Starts `rmw_zenohd` as a local router on `localhost:7447`. All ROS nodes on this machine connect to it.

### Satellite mode (cross-machine ROS)

```zsh
./start_zenoh_router.zsh <robot-ip>
```

- Checks TCP reachability to `<robot-ip>:7447` (5s timeout)
- If reachable: stops the ROS daemon, then starts `rmw_zenohd` with `connect/endpoints=["tcp/<robot-ip>:7447"]`
- This bridges the local machine's ROS graph to the robot's router — useful for dev machines connecting to a running robot

Example:

```zsh
./start_zenoh_router.zsh 192.168.50.1
```

---

## Ethernet Interface (`enP8p1s0`)

Configured by `scripts/update/configure_hardware.sh` (runs in post-update):

- Creates a static NetworkManager profile `jetson-eth`
- Interface: `enP8p1s0` at `192.168.50.2/24`
- Non-default-route (won't hijack default gateway)
- Used for direct SSH/LAN access when connected to the robot on the same subnet

No manual setup needed — post-update handles it.
