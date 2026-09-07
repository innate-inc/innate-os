# Crossroads

An original 40 m town square with four buildings, a fountain plaza, trees,
benches, flush curb cuts, zebra crossings, and deterministic right-hand traffic.
Select **Crossroads** in the simulator environment menu.

All scenery is authored as primitives in `sim/tools/build_intersection.py`.
There are no downloaded inputs, external textures, or purchased asset dependencies.
Source and generated scenery are
Apache-2.0; attribution ships in both OCI layers.

The normal asset-image build generates the browser GLB, exact convex collision
meshes, MuJoCo visuals, and a lidar-scanned static navigation map. Cars are
parked out of the scan so their initial positions do not become map obstacles.
The launcher installs this pack like Apartment and Backrooms; no local asset
override is needed once CI publishes the checkout's asset image.

To regenerate locally from the repository root:

```sh
sim/.venv/bin/python sim/tools/build_intersection.py
```

For an isolated build, pass `--assets-dir /tmp/crossroads/assets` and
`--viewer-out /tmp/crossroads/viewer`. Driving surfaces are at z=0 because
MARS's planar base cannot climb; border curbs have flush cuts at every crossing.
The map is for static localization, not
traffic-light-aware route planning: moving cars are live obstacles, and the
robot's driving policy must obey the signals.
