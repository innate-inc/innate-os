# Innate Local CLI

This directory holds the implementation of the local `innate` CLI. User-facing configuration no longer lives here.

The local workflow uses:

- [`.env`](/Users/axelpeytavin/Projects/innate-repos/innate-os/.env) for secrets only
- [`config/os.toml`](/Users/axelpeytavin/Projects/innate-repos/innate-os/config/os.toml.template) for optional non-secret OS overrides
- [`sim/config.toml`](/Users/axelpeytavin/Projects/innate-repos/innate-os/sim/config.toml.template) for optional non-secret simulator overrides

The CLI brings up:

- `innate-os` in its Docker-based simulation setup
- `sim/` on the host, serving the built frontend on `http://localhost:8000`
- an optional local `innate-cloud-agent`

By default, the launcher expects this layout:

```text
innate-os/
├── innate
├── stack                      # deprecated compatibility alias
├── .env
├── config/os.toml
├── dev/launcher/
├── sim/config.toml
└── ../innate-cloud-agent/   # optional
```

## Quick Start

```bash
cd innate-os
./innate sim setup
./innate sim up
```

If any local config file does not exist yet, the CLI creates it from its template automatically.
`./innate sim setup` prepares the Python environment, builds the simulator frontend, and downloads the required ReplicaCAD scene datasets into `sim/data/` when needed. This requires Git LFS (`brew install git-lfs && git lfs install` on macOS).
On interactive terminals, `./innate sim up` drops into a live dashboard after startup. It keeps the simulator, agent, and brain logs visible together and adds a `btop`-style metrics band at the top. Use `d` to toggle the simulator's real runtime log mode between `quiet` and `debug` without restarting, `q` to leave the dashboard while keeping the runtime running, and `Ctrl+C` to stop the full runtime.

If you want the native simulator viewer window for a run:

```bash
./innate sim up --vis
```

If you just want a one-shot startup plus a single status snapshot:

```bash
./innate sim up --once
```

To stop everything:

```bash
./innate sim down
```

To inspect the current state:

```bash
./innate sim status
./innate sim status verbose
./innate sim logs startup
./innate sim logs brain
./innate sim logs simulator
```

`./stack ...` still works as a deprecated compatibility alias and forwards into `./innate sim ...`.

## Config Files

[`config/os.toml`](/Users/axelpeytavin/Projects/innate-repos/innate-os/config/os.toml.template) is for optional non-secret OS overrides such as:

- brain websocket URI
- telemetry URL
- Cartesia voice id

[`sim/config.toml`](/Users/axelpeytavin/Projects/innate-repos/innate-os/sim/config.toml.template) is for optional non-secret simulator overrides such as:

- native viewer on/off
- hosted vs local cloud-agent mode
- local cloud-agent image or source checkout

Everything else uses built-in defaults.

## Notes

- The CLI uses the `sim/` frontend build instead of a separate Vite dev server so the runtime stays self-contained.
- `./innate sim setup` always bootstraps the simulator environment, frontend build, and required scene data when needed.
- `sim/config.toml` can make the simulator start with its native viewer window by default, while `./innate sim up --vis` is the one-run override.
- The simulator starts in quiet log mode by default. Press `d` in the dashboard to switch between `quiet` and `debug` live.
- `status` opens as a dashboard panel with simulator logs, local agent logs, and the OS brain pane side by side when your terminal is wide enough.
- `up` now lands in a live-refreshing version of that dashboard with a charted metrics band for health, FPS, queue load, and frame age.
- The dashboard now switches to a more compact header on medium-height terminals so the top of the frame does not get pushed off-screen.
- The dashboard now renders log lines as-is, including ANSI colors from the source process. It no longer rewrites or recolors the log content.
- The transport numbers are queue-depth estimates for the sim/OS bridge, not byte-level network throughput yet.
- `logs startup` shows the captured startup logs, while `logs brain` pulls live output from the brain tmux pane inside the OS container.
