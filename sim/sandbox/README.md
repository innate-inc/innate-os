# sim/sandbox + sim/tools

Native MuJoCo dev tooling for the virtual MARS: the offline asset pipeline
that generates the apartment collision/visual meshes, plus interactive
sandboxes for physics debugging.

The virtual MARS driver itself (the headless sim `./innate-sim up` launches,
impersonating the real hardware drivers at the ROS topic level) lives in the
ROS package `ros2_ws/src/mars_bot/mars_sim_driver` -- see its docstrings.
Model building (world XML, URDF attach, planar base, gains) is shared: the
sandboxes import it from `mars_sim_driver.world` via the `_driver_pkg.py`
bootstrap, so there is one implementation.

sim/viewer (the SimSession library the webapp embeds) renders the same world
by mirroring the driver over rosbridge; the standalone browser sim (MuJoCo
WASM, no ROS) lives in the separate innate-sim-demo repo.

## Layout

```
sandbox/
  _driver_pkg.py         sys.path bootstrap for importing mars_sim_driver
  common.py              spawn/drive servo helpers, stress tour, viewer camera launcher
  drive_mars.py          real-URDF WASD sandbox (browser control panel)
  control_panel.html     browser control panel for drive_mars.py
  stress_test_apartment.py  headless stability gate -- run after ANY collision
                            mesh or contact-parameter change
  test_driver_core.py    headless check of mars_sim_driver.core (no ROS needed)
  virtual_mars_demo.ipynb   VirtualMars walkthrough notebook: rgb/depth obs,
                            lidar, occupancy grid, driving, arm -- no ROS
                            (`uv sync --group notebook`, kernel = sim/.venv)

tools/                   asset pipeline (one-time generation -> sim/assets/)
  split_apartment_obj.py     apartment OBJ -> per-room OBJs
  decompose_rooms.py         CoACD convex decomposition (collision hulls)
  bake_all_rooms.sh          decompose all rooms in parallel
  export_visual_rooms.py     textured room meshes for MuJoCo's renderer
  export_nav_map.py          nav2 map_server map from the collision world
  decompose_objects.py       CoACD hulls for the standalone props (dog, ball)
  build_viewer_physics.py    repack the browser hulls from the collision store
```

## Setup

```bash
cd sim
uv sync
uv run sandbox/test_driver_core.py   # headless smoke test, saves camera frames to sim/assets/virtual_mars_test/
```

## The virtual MARS driver

See `ros2_ws/src/mars_bot/mars_sim_driver`: `core.py` is the transport-free
sim (physics, servos, cameras, lidar, depth), `node.py` the ROS 2 node
exposing the real driver topic surface, and `world.py` the shared model
building. Run it with `ros2 launch mars_sim_driver sim_driver.launch.py`
(what `./innate-sim up` does inside the container); test the core anywhere
with `uv run sandbox/test_driver_core.py`.

Apartment meshes come from sim/assets/ (gitignored): `./innate-sim up`
extracts the geometry layer of the published asset image; to change the
geometry, edit tools/ (see below) and push -- CI rebuilds the image.

## Asset pipeline

The apartment mesh is unwelded triangle soup, so collision geometry is
generated, not used raw:

```bash
cd sim/tools
uv run split_apartment_obj.py        # per-room OBJs -> sim/assets/apartment_split/
./bake_all_rooms.sh                  # CoACD hulls -> sim/assets/apartment_split_v2/ (hours)
uv run export_visual_rooms.py        # textured rooms -> sim/assets/apartment_visual/
```

**After any regeneration, run `uv run sandbox/stress_test_apartment.py`
before trusting the result** -- hull seams can corner-catch into divergence.
Editing anything under tools/ moves the asset image's `inputs-<hash>` tag, so
pushing is what publishes: CI regenerates the whole store from the pinned raw
geometry (including the viewer's flat hull copy + manifest, repacked by
tools/build_viewer_physics.py). Regenerating locally is for validating the
result -- the published image is always built by CI, since sim/assets is
gitignored and never reaches a runner any other way.

### Tuning notes (hard-won, don't rediscover)

- **CoACD preprocess_resolution** (tools/decompose_rooms.py): the rooms
  aren't watertight, so CoACD voxel-remeshes them first; the default
  resolution of 50 inflates every surface ~3.5cm (the "buffer" around
  furniture). 200 measures ~1.1cm median / 3.5cm max, 400 ~0.8cm / 2.6cm --
  maxima land at rounded furniture corners. Each doubling makes the bake
  several times slower; bake rooms in parallel.
- **Contact margin 0.007** (mars_sim_driver.world's build_world_xml; the
  innate-sim-demo worker mirrors it): the margin also bridges hull seams. At
  3-5mm one seam corner-catches into a 20+ m/s single-step spike; 0.007 ran
  5 stress-tour loops clean. Don't lower it without re-gating.
- **implicitfast integrator**: what makes 1200+ hulls stable at all --
  damps single-step seam impulses that explode under Euler.
- **The robot's own collision geometry** lives in `mars.urdf` as named
  `<collision>` primitives -- one description the driver, MoveIt and the
  viewer's "collisions" overlay all read. MuJoCo-only contact settings (grasp
  parameters, frictionless drive wheels) stay in `world.tune_contacts`.
- **SDF shells were tried and removed** (git history has them, last at
  `build_sdf_shells.py`). MuJoCo >= 3.3.5 builds octree SDFs natively -- no
  hulls, no seams, ~2min bake -- but the SDF sign is brutally sensitive to mesh
  topology: raw meshes and binary marching-cubes output (non-manifold corner
  junctions) both cause phantom deep-penetration impulses (robot flung
  100+ m/s, ncon=0 either side). Gaussian-smoothed marching cubes plus
  topology-preserving decimation survived a 5-loop stress tour, at the cost of
  a ~292 MB pymeshlab dependency in the builder image. Worth reviving only with
  a consumer attached.

## Sandboxes

- `uv run sandbox/drive_mars.py` -- interactive viewer with the real URDF;
  drive from http://localhost:8766/control_panel.html (WASD/joystick over a
  local WebSocket -- macOS mjpython limitations rule out viewer-native keys).
