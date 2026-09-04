# sim/demo — the public sim demo image

One self-contained container that **is** a full sim session: start it, route
port 80, throw it away. No bind mounts, no host process, no launcher — which is
what makes it schedulable per session on Fly Machines, Cloudflare Containers, or
a slot on your own box.

```bash
sim/demo/build.sh                 # -> ghcr.io/innate-inc/innate-os-sim-demo:<sha>
docker run --rm -p 8080:80 ghcr.io/innate-inc/innate-os-sim-demo:latest
# then open http://localhost:8080
```

## How it differs from the dev sim

`./innate-sim up` runs the ROS fleet in a container and the MuJoCo world
**natively on the host**, because native GL renders ~7x faster than software GL
in Docker. A headless cloud instance has no native GL either way, so the demo
hosts the world in the same container via the escape hatch
`mars_sim_driver/launch/sim_driver.launch.py` documents:
`VIRTUAL_MARS_REMOTE=127.0.0.1:8799`.

Everything else follows from being public:

| | dev sim | demo |
|---|---|---|
| source, assets, viewer | bind-mounted from a checkout | baked in |
| world server | host process (`uv`) | in-container |
| `.env`, `~/.ssh`, `~/.gitconfig` | mounted | **absent** |
| `POST /settings.json`, `/restart` | on | **off** (`INNATE_WEBAPP_READONLY=1`) |
| foxglove, leader receiver | on | off |
| lifetime | until `down` | `INNATE_DEMO_LEASE_SECONDS` (600) |
| render scale | 1 | 2 |

## The two numbers that decide whether this works

**Render speed.** Software GL is the demo's quality risk. The world server logs
its measurement on every boot:

```bash
docker logs <container> 2>&1 | grep "GL self-test"
```

`GL self-test (osmesa): N ms/frame`. Cameras render on demand at ~8Hz, so N much
above ~120ms starves the camera panel and the agent's vision. Levers, in order:
rebuild with a higher `INNATE_SIM_RENDER_SCALE` (cost falls with the square),
give the instance more cores, or move to a GPU instance and set `MUJOCO_GL=egl`.

The scale is a **build-time** choice: it sets the texture cap baked into the
compiled model, so changing it at run time would miss the `.mjb` cache. The
entrypoint refuses that rather than booting slowly for reasons nobody can see.

**Boot time.** From `docker run` to a usable app, with the `.mjb` baked in.
Budget ~30–60s for the ROS fleet. Anything much longer means the model cache
missed — check the build log for `model cache:` and confirm no `COPY` was added
after the prewarm step (the cache key hashes every asset's mtime+size).

## Deploying it

The image is hardened; handing out sessions, keys, limits and the challenge are
the broker's job: `apps/sim-broker` in innate-cloud.
