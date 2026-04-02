# Local Stack Launcher

This directory powers the one-command local stack launcher for this monorepo. It gives you one `.env` and one command for bringing up:

- `innate-os` in its Docker-based simulation setup
- `sim/` on the host, serving the built frontend on `http://localhost:8000`
- an optional local `innate-cloud-agent`

By default, the launcher expects this layout:

```text
innate-os/
├── stack
├── dev/launcher/
├── sim/
└── ../innate-cloud-agent/   # optional
```

If your repos live elsewhere, set the path overrides in `.env`.

## Quick Start

```bash
cd innate-os
./stack up
```

If `.env` does not exist yet, the launcher creates it from `.env.template` automatically.
On interactive terminals, `up` now drops into a live dashboard after startup. It keeps the simulator, agent, and brain logs visible together and adds a `btop`-style metrics band at the top. Use `d` to toggle the simulator's real runtime log mode between `quiet` and `debug` without restarting, `q` to leave the dashboard while keeping the stack running, and `Ctrl+C` to stop the full stack.

If you want the native simulator viewer window for a run:

```bash
./stack up --vis
```

If you just want a one-shot startup plus a single status snapshot:

```bash
./stack up --once
```

To stop everything:

```bash
./stack down
```

To inspect the current state:

```bash
./stack status
./stack status verbose
./stack logs startup
./stack logs brain
./stack logs simulator
```

## Cloud Agent Modes

Set `STACK_CLOUD_AGENT_MODE` in `.env`:

- `hosted`: do not run a local cloud agent. The OS uses its default hosted brain URL unless you explicitly set `BRAIN_WEBSOCKET_URI`.
- `local-image`: run a local image defined by `STACK_CLOUD_AGENT_IMAGE`.
- `local-source`: build and run from `STACK_CLOUD_AGENT_DIR`.

Local cloud-agent modes automatically override the OS brain URL to `ws://host.docker.internal:$STACK_CLOUD_AGENT_PORT`.

## Notes

- The launcher uses the `sim/` frontend build instead of a separate Vite dev server so the stack stays one-command.
- `STACK_OS_ALWAYS_BUILD=true` rebuilds the ROS workspace on each `up`. It is slower, but it keeps the UX reliable while we evaluate this setup.
- `STACK_SIM_AUTO_SETUP=true` and `STACK_SIM_AUTO_BUILD_FRONTEND=true` make first launch smoother by bootstrapping the simulator environment if needed.
- `STACK_SIM_VISUALIZATION=true` makes the simulator start with its native viewer window by default, while `./stack up --vis` is the one-run override.
- `STACK_SIM_LOG_MODE=quiet` starts the simulator with noisy debug chatter suppressed at the source. Press `d` in the dashboard to switch between `quiet` and `debug` live.
- `status` opens as a dashboard panel with simulator logs, local agent logs, and the OS brain pane side by side when your terminal is wide enough.
- `up` now lands in a live-refreshing version of that dashboard with a charted metrics band for health, FPS, queue load, and frame age.
- The dashboard now renders log lines as-is, including ANSI colors from the source process. It no longer rewrites or recolors the log content.
- `INNATE_BRAIN_LOG_PROFILE` controls how concise the brain logs are before the launcher captures them. Use `message-only`, `compact`, or `ros-default`.
- The transport numbers are queue-depth estimates for the sim/OS bridge, not byte-level network throughput yet.
- `logs startup` shows the captured startup logs, while `logs brain` pulls live output from the brain tmux pane inside the OS container.
