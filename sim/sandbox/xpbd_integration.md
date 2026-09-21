# Simulator cloth: cotton and wool socks

The default XPBD backend advances cloth alongside MuJoCo's robot and rigid
bodies. Scene setup offers **Soft sock** (25 g cotton) and **Wool sock** (60 g).
These are tuned interactive approximations, not calibrated material models.

## Build and run

From `sim`, with Python 3.11+, git, CMake, C++17 and OpenMP available:

```sh
uv run --python 3.11 --extra xpbd python sandbox/build_xpbd.py
```

On macOS the helper expects Homebrew LLVM. It pins PositionBasedDynamics and
Eigen, applies the headless-build and batch-state patches, and writes the native
module and upstream license under ignored `sim/.xpbd/lib`. Building requires
internet access; the module must match the world-server Python interpreter.
No system packages or OS images are modified.

From the repo root, run `./innate-sim up` (or `--offline` with cached assets).
The native build is an explicit prerequisite; missing dependencies fail with a
build instruction rather than silently changing cloth behavior.
`INNATE_SIM_XPBD_MODULE_DIR` can select another compatible native build.
`INNATE_SIM_CLOTH_BACKEND=mujoco` explicitly selects the legacy fallback.
Use a free `INNATE_SIM_PORT_BASE` when another clone is running.

## Runtime and limitations

- Robot and cloth advance synchronously at 300 Hz while either sock is active;
  normal 500 Hz stepping resumes when neither is present.
- Every prop owns independent native state. Reset, remove and reposition clear
  stale motion; native global-clock access is serialized.
- Contacts sample cloth vertices, edges and faces against convex rigid geometry.
  Reaction is applied to finger hinges only; other rigid bodies are one-way
  colliders. Unsupported collision shapes and nonstandard gravity fail explicitly.
- Self-contact is inexpensive particle repulsion, not triangle collision or CCD.
  Self-intersections remain possible. Separate socks do not collide with each other.
- Cotton retains its flat rest metric and compliant bending. Wool adds sparse
  panel-distance bending supports and 2.5 mm contact separation (cotton: 0.25 mm).
  These supports also stiffen in-plane deformation; there are no grasp pins,
  world-space shape targets or volume-preservation constraints.
- Each sock has 133 simulated vertices. Browser-only subdivision gives cotton
  4,096 triangles. Wool adds inner/outer surfaces and an open-cuff rim, totalling
  8,256 triangles and 3.6 mm visual fabric thickness. Offset surfaces may intersect.
  Native robot cameras render the coarse physical mesh, not this visual shell.

## Verification

From `sim`, after installing assets and building the native module:

```sh
OMP_NUM_THREADS=1 PYTHONPATH=.:sandbox uv run --extra xpbd --with pytest python -m pytest \
  test_build_soft_sock_asset.py test_cloth_fast_contact.py test_soft_sock_binding.py \
  test_xpbd_integration.py test_wool_sock.py ../tests/test_xpbd_launcher.py
OMP_NUM_THREADS=1 PYTHONPATH=.:sandbox uv run --extra xpbd python sandbox/check_xpbd_world.py \
  --prop wool_sock --output /tmp/wool-check --no-video
```

Use a new output directory and repeat with `--prop soft_sock`. This isolated
full-apartment benchmark checks placement, stream data, hold/move/release, reset
and timing. It starts folded between open jaws: it is not autonomous floor pickup.
It never starts, stops or resets a user's running service.
`cloth_fixture.py` supplies the real URDF fingers for focused integration tests.

In `sim/viewer`, run `npm test && npm run typecheck && npm run build:lib`.
For visual inspection, export a benchmark trajectory with
`sandbox/export_sock_visual_trial.py` to
`viewer/node_modules/.cache/sock-visual.json`, serve the viewer with Vite, and
open `/test/cloth-visual.html?wool`. Both sides replay the same motion to compare
appearance, not material physics. Spawn/remove/re-add is checked on that page.
`check_xpbd_stream.py` is an optional websocket/RPC check: point it only at a
dedicated test server, since it changes that server's scene.
