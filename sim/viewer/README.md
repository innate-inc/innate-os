# sim/viewer

The Three.js **SimSession library** the webapp embeds when the robot is
simulated: a live, full-resolution, drag-to-orbit 3D view of the apartment +
MARS robot, fed by the world server's ground-truth observer stream (~75Hz
pose + joints over a WebSocket, a few KB/s -- no video pipe): direct to
loopback when the page is local, else the webapp's proxied `/worldstate`
route. Physics never runs in the browser; this is rendering only. Only the
lidar debug overlay reads a robot topic (`/scan` over rosbridge, connected
on first toggle), because it deliberately shows what the robot senses.

The standalone browser sim (MuJoCo WASM physics, no ROS) lives in the
separate `innate-sim-demo` repo -- it was extracted from this source tree
and keeps its own copy.

The stage has sim-only chips: **lidar** (live `/scan` hit points),
**collisions** (wireframe of everything the driver collides against, for
physics-vs-visual alignment checks: the apartment hull set plus the robot's
own `<collision>` primitives, which urdf-loader parses out of mars.urdf and
hangs off each link so they track the joints), and a **prop** row per set.

The apartment has no props in it by default -- every one of them starts parked
off-map. A prop chip puts one in the world, and the **drop | at robot** switch
says how: "at robot" sets it down at rest at its own tuned reach offset (drive
somewhere, lay a set out, practise grabbing), while "drop" takes over the
pointer so you click a spot and drag a heading, and the prop falls onto
whatever is under it. A set chip (`+manipulation`) lays out a whole group at
once. **clear** sends every prop back off-map, which is also what a sim reset
leaves behind.

`SimSession` also relays the world server's challenge judge
(`onChallenge`/`startChallenge`/`abortChallenge`, see
`mars_sim_driver/challenges.py`): the roster arrives once per connection, the
run state rides the stream, and the session merges the halves so a renderer
sees one view. The webapp's challenge panel is the only consumer, and it keys
off `onChallenge` existing to stay sim-only — nothing here judges anything.

The render assets (`public/models` glb, `public/physics` hulls for the
overlay) are not in git: `./innate-sim up` mounts them straight out of the
asset image. `/robot` is different -- it is served directly from
`ros2_ws/src/mars_bot/mars_sim`, the tracked source, so a `mars.urdf` edit
reaches the browser with no copy step and no republish. That is also why
`loader.packages = { mars_sim: "/robot" }` in scene.ts is literally true:
`/robot` IS the package.

## Build

**Running the sim never needs Node.js, even while editing this directory.**
The bundle is compiled by [`Dockerfile`](Dockerfile) into its own image, which
compose mounts at `/sim-viewer/`. Docker is the only prerequisite, and the sim
already requires it.

Published as `ghcr.io/innate-inc/innate-os-sim-viewer:inputs-<hash>`, computed
over this directory as it is on disk -- git decides membership (`ls-files
--cached --others --exclude-standard`), so a new unadded `.ts` counts and
node_modules does not. Separate from the asset image because this directory
changes constantly and the geometry takes hours to rebuild.

Edit anything here and the hash names an image CI cannot have published. `./innate-sim up` notices and builds the same Dockerfile
locally instead, tagging it `innate-os-sim-viewer-local:inputs-<hash of these
files>` — so an unchanged tree skips the rebuild, and superseded tags are
pruned. Build output goes to `sim/launcher/.state/logs/viewer-build.log`.

The npm scripts are for working on the viewer directly, not for running the sim:

```bash
npm install
npm run build:lib   # dist-lib/sim-session.js -- NOT what the container serves
npm run typecheck
npm test           # generated resident assets + prop animation lifecycle
```

`dist-lib/` on the host is a scratch output of those scripts. Nothing mounts it.

The webapp loads `/sim-viewer/sim-session.js` dynamically when `/robot_info`
reports `simulated: true`; real robots never request it (they use WebRTC),
and nothing here is ever installed on a robot.

## Layout

```
src/
  simSession.ts     Session facade (WebRtcSession-compatible state shape),
                    jitter-sized interpolated playback of the state stream
  simStage.ts       Mounts the canvas, render loop (60fps cap), PiP thumbnails
  scene.ts          Three.js scene: apartment glb + URDF robot, cameras
  props.ts          Prop roster -> models the scene draws + stage buttons
  physics/worldStateController.ts  observer-stream client (auto-reconnect)
  physics/rosbridgeController.ts   /scan overlay source (lazy-connected)
public/             (fetched bundle) robot URDF+STLs, apartment glb
```

Scene convention is Z-up, X-forward (matches ROS/REP-103), so the robot's
URDF loads with no axis remap; the apartment glb (authored Y-up, the glTF
convention) is rotated on load.

## Household residents

`tools/residents/model.mjs` authors Alex, Blake, and Casey with a 24-second
seamless idle cycle. `tools/residents/head.mjs` shapes their individual heads,
sculpted facial contours, eyes, lips, hair, and subtle skin shading.
`tools/build-residents.mjs`
generates both renderer formats from that source during the asset-image build:
animated GLBs and standing-pose MuJoCo OBJ/palette textures. They are normalized
to the existing resident heights, Z-up with feet at the origin and facing +Y,
so the household challenge's placement/yaw and dialogue remain unchanged.

For local asset inspection, run `node tools/build-residents.mjs /path/to/output`;
it writes `models/` and `humans/`. Generated files stay outside git. Static face
and hair details are merged by material to limit draw calls. Vertex colors are
baked into palette swatches for matching skin and beard shading in MuJoCo.

The sidecar's `viewer.idleAnimation` opts into the GLB's `Idle` clip. PropLibrary
samples it at interpolated simulation time, so pause, reset, and stream stalls
hold the same pose as the rest of the scene. Placement previews stay still.
Idle motion is cosmetic in Three.js; MuJoCo RGB/depth use the same characters
in their standing pose, with fixed collision hulls. This does not add roaming
or walking to the household challenge. Old bundles without the clip still
display their static models.
