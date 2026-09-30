# CI (`ci/`)

How innate-os is built and tested in CI, and *why* it's wired this way. The
table below is the gate-by-gate breakdown.

## Layout

| File | Role |
|---|---|
| `Dockerfile.test-base` | **Warm base** (published to ghcr from main): apt/pip deps, rosdep, a full prebuilt `ros2_ws` (`build/`+`install/`), and a *populated ccache*. Changes rarely. |
| `Dockerfile.test` | Thin CI image, `FROM` the warm base: overlays this revision's source + `config/`/`workspace/` and does an **incremental, ccache-backed** `colcon build` (only changed packages recompile; the whole tree is still built). |
| `Dockerfile.test-base.dockerignore` / `Dockerfile.test.dockerignore` | Build contexts for the two images (allowlist of what each COPYs). Each Dockerfile needs its own — BuildKit otherwise falls back to the restrictive root `.dockerignore`. |
| `run_integration_tests.sh` | **Single source of truth for "the tests."** Runs unit (pytest) + integration (`colcon test`) inside the image. |
| `docker-compose.test.yml` | Runs `run_integration_tests.sh` locally / on a self-hosted box. |
| `cloudbuild.integration-test.yaml` | Runs the *same* script on Google Cloud Build. |

The GitHub workflows live in `../.github/workflows/` (`format.yml`, `integration-test.yml`).

## Why tests run on Cloud Build

`integration-test.yml` runs on a GitHub-hosted runner that does **no Docker work**
— it just authenticates to GCP via **Workload Identity Federation** (OIDC, no
service-account keys) and `gcloud builds submit`s the heavy build+test to **Cloud
Build** (`E2_HIGHCPU_8`). This keeps the slow ROS image build off any single
machine while staying on GCP. The build and the run-tests step both use the
documented `gcr.io/cloud-builders/docker` form.

`run_integration_tests.sh` is shared so local and CI run the identical flow —
there is no second definition of "the tests" to drift.

## Why Zenoh shared memory is disabled in CI

This is the non-obvious one. `rmw_zenoh` allocates a **POSIX shared-memory
provider at `rclpy.init`** (~48 MiB/node). Cloud Build's default workers run under
**gVisor, which blocks POSIX shm** — so every ROS node dies at startup with
`Failed to create POSIX SHM provider` (`OS error 12`), *even with a 2 GB
`/dev/shm`*. It is not a `/dev/shm` size problem.

So `run_integration_tests.sh` exports `ZENOH_*_CONFIG_OVERRIDE=...shared_memory/enabled=false`
for the test run. These are **correctness tests** (orchestration logic, not
transport), so loopback is equivalent to SHM. The **robot keeps SHM in production**
— it's enabled by the image entrypoint, which the test script overrides only for
itself.

`--shm-size=2g` in `cloudbuild.integration-test.yaml` and the optional
`GCP_CLOUD_BUILD_WORKER_POOL` (a private, non-gVisor worker pool) are belt-and-
suspenders: they only matter if SHM is ever re-enabled in CI.

## Why the build covers every package

The build covers **all** packages (no `--packages-skip`). `mars_cam`/`nav`/
`control` were once skipped as "Jetson-only", but they compile fine off Jetson
(cam's VPI stereo is `if(VPI_FOUND)`-guarded; nav's `cupy` is runtime-only).
Building everything makes the build the broad safety net for structural refactors.

## Why the image is split into a base + thin layer

Cloud Build VMs are ephemeral, so without help every run paid ~180s to apt/pip
install ~1000 packages **and** a full `colcon build` from scratch. The split fixes
both while still building the whole tree:

- `Dockerfile.test-base` bakes the deps + a full prebuilt `ros2_ws` + a populated
  ccache, and is pushed to `ghcr.io/innate-inc/innate-os-test-base:{main,buildcache}`
  **only on main** (`_PUBLISH_BASE=true`).
- PR builds `FROM` that base, so they skip apt/pip entirely and the ccache (which
  is content-addressed, so it survives a fresh checkout) turns unchanged
  translation units into hits — only changed packages do real compile work.

A cache-missed `colcon` layer can never see the ccache it would itself produce, so
`--cache-from` alone can't make it incremental — the *published* base is the load-
bearing piece. See `cloudbuild.integration-test.yaml` for the main-vs-PR flow.

## Running it locally

```bash
# Build the warm base once (deps + full prebuilt ros2_ws + ccache), then the thin
# image on top. CI publishes the base from main; locally you build it yourself.
docker build -t ghcr.io/innate-inc/innate-os-test-base:main -f ci/Dockerfile.test-base .
docker build -t innate-os-test:latest -f ci/Dockerfile.test .

INNATE_TEST_IMAGE=innate-os-test:latest docker compose -f ci/docker-compose.test.yml up \
  --abort-on-container-exit --exit-code-from integration-test
```
