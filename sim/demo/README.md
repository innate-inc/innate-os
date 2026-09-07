# sim/demo — the public sim demo image

One container that is a whole sim session, world server included: start it,
route port 80, throw it away. The broker in innate-cloud (`apps/sim-broker`)
hands one to each visitor of sim.innate.bot and destroys it after the lease.

```bash
sim/demo/build.sh          # -> us-central1-docker.pkg.dev/innate-managed-infra/sim/innate-os-sim-demo:<sha>
docker run --rm -p 8080:80 --network sim-demo \
  -e INNATE_DEMO_PROXY_URL=http://sim-credential-relay:8081 \
  us-central1-docker.pkg.dev/innate-managed-infra/sim/innate-os-sim-demo:latest
```

The `sim-demo` network above must already contain the trusted credential relay.
In production the broker provisions that separately and restricts access with
network policies. The relay holds the service/provider keys and authenticates
upstream; the visitor container sends no bearer token. Do not publish the
relay port or mount credentials into a session to bypass a failed startup.

The image sets `INNATE_PUBLIC_DEMO=1`. Startup refuses credential environment
variables, owner `.env` / `/etc/innate.env` files, and owner `settings.yaml`.
Public configuration must provide `INNATE_DEMO_PROXY_URL`; owner direct-provider
and authenticated-proxy configuration continues to work in the developer sim.
Deploy this image with the matching credential-relay broker changes.

Every push to main publishes `sha-<commit>` and moves `latest`
(`.github/workflows/publish-sim-demo.yml`); the broker's `/admin` page picks
which tag sessions run.

## How it differs from the dev sim

`./innate-sim up` keeps the MuJoCo world on the host for native GL. A cloud
instance has no native GL either way, so the demo runs the world in-container
(`VIRTUAL_MARS_REMOTE=127.0.0.1:8799`) under software GL.

| | dev sim | demo |
|---|---|---|
| source, assets, viewer | bind-mounted from a checkout | baked in |
| world server | host process (`uv`) | in-container |
| `.env`, `~/.ssh`, `~/.gitconfig` | mounted | **absent** |
| service/provider credentials | operator-owned | **absent**, external relay |
| `POST /settings.json`, `/restart` | on | **off** (`INNATE_WEBAPP_READONLY=1`) |
| foxglove, leader receiver | on | off |
| lifetime | until `down` | `INNATE_DEMO_LEASE_SECONDS` (600) |
| render scale | 1 | 2 |

## The two numbers that decide whether it works

**Render speed.** The world server logs `GL self-test (osmesa): N ms/frame` on
boot. Cameras render on demand at ~8Hz, so N much above ~120ms starves the
camera panel and the agent's vision. Levers: a higher `INNATE_SIM_RENDER_SCALE`
(cost falls with the square), more cores, or a GPU instance with `MUJOCO_GL=egl`.
The scale is baked into the compiled model at build time; the entrypoint refuses
a different one rather than booting slowly for reasons nobody can see.

**Boot time.** From container start to a usable app: ~30s, with the `.mjb`
baked in. Much longer means the model cache missed -- check the build log for
`model cache:` and that no `COPY` was added after the prewarm step.
