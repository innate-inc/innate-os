# Innate Webapp

Web cockpit for the Innate robot — one zero-build app whose pages share the
same modules:

- **Agent** (`/`) — autonomous control, camera views, and the Brain monitor.
- **Teleop** (`/teleop`) — live video, joystick/keyboard drive, head tilt,
  robot speech, telemetry, and leader-arm USB follow.
- **Nav** (`js/nav/`) — live navigation view: the map widget with laser scan /
  global costmap / traveled-path overlays, telemetry panels (pose, velocity,
  lidar, nav state, per-topic receive rates), and live strip charts (commanded
  vs measured velocity, nearest obstacle).
- **Collect** (`collect/`) — record episodes (learned skills) and one-shot
  recorded movements; reuses the teleop cockpit with a recording HUD.
- **Datasets** (`datasets/`) — browse a skill's episodes and replay them
  (per-camera MP4 + synced joint graph).
- **Training** (`training/`) — start and monitor cloud training runs: live
  step progress, ETA, logs, and the W&B link.
- **Logging** (`logging/`) — a live, structured view of the robot's full
  console (the `innate_console` stream).

## Run

Served by the robot over HTTPS — one process serves the app *and* proxies
`/ws` to rosbridge on the same TLS port (the client switches to `wss://`
automatically):

```sh
python3 proxy/https_server.py        # https://mars.local:4443 (or robot IP)
```

Pages served by the robot auto-connect to it (no IP typing). The certificate
is self-signed (generated on first run) — accept the browser warning once per
machine. A secure origin is what unlocks **leader-arm USB teleop** from the
robot-served page. There is no build step, no install, no `node_modules`.

On the robot it starts automatically in the `console-webapp` tmux window — see
[scripts/launch_ros_in_tmux.sh](../scripts/launch_ros_in_tmux.sh).

## Stack

Zero-build static site: `index.html` + native ES modules + one hand-written
stylesheet. Typing comes from JSDoc + `// @ts-check` against shared interfaces
in [js/types.d.ts](js/types.d.ts) — TypeScript is used as a *checker*, never a
compiler:

```sh
npx tsc --noEmit      # optional type check (editor gets it for free)
```

Each page is a sibling sharing those modules. If a page one day genuinely
needs a chart library or framework, that page adopts it locally — never
app-wide tooling.

### Load speed without a bundler

No bundler means no dead-code elimination and no minification, so the first load
stays fast only by keeping bytes and round trips deliberate. Four rules do that:

- **The front door gzips text assets** and caches the compressed bytes per file
  identity, so a cold load moves ~350 KB instead of ~1.8 MB. The pinned vendor
  libraries under `public/vendor/` are additionally served immutable — their
  filenames carry the version (`three.module.min.r160.js`) so a bump is a new
  URL; an unversioned vendor file stays no-cache rather than pinning stale
  bytes in every returning browser for a year.
- **`index.html` names every first-paint module** in `<link rel="modulepreload">`.
  Without it the import graph is discovered a level at a time — five serial
  round trips before the page module is even requested. `node tests/preload.test.js`
  recomputes the graph and reports any drift.
- **Anything heavy that a page might not show is a dynamic `import()`** — the map
  widget, the Brain monitor, the Arm SDK's three.js view. Static-importing them
  puts them on every visitor's critical path.
- **The router warms only the cockpit neighbours (teleop, nav), after the first
  page has mounted.** A browser counts the main thread as idle while the first
  page waits on the network, so an earlier `requestIdleCallback` races the page
  the user wants — and warming all twelve routes billed every load ~140 KB for
  pages most sessions never open. An unwarmed route is one import away.

## Layout

```
index.html              shared SPA shell (Agent is the default route)
js/*/main.js            page modules mounted by the client router
css/app.css             entire design system
proxy/https_server.py   HTTPS front door: app + wss rosbridge proxy + episode media
js/
  constants.js          robot topic names
  rosClient.js          shared rosbridge socket (reconnect, sub replay, services)
  driveController.js    /joystick heartbeat + joystick/keyboard arbitration
  webrtcSession.js      camera + mic over WebRTC, signaled through rosbridge
  sharedVideoSession.js one app-level WebRtcSession shared by every video page
  dynamixel.js          leader-arm WebSerial reader (Protocol 2.0)
  shell.js              icon rail on every page
  railLayout.js         the rail's grouped roster (pure — tests/railLayout.test.js)
  router.js             client-side routing, boot splash, background route warm-up
  teleop/               teleop modules (joystick, keyboard, head tilt, TTS, arm)
  nav/ collect/ datasets/ training/ logging/      per-page modules
```

## Robot interface (rosbridge `ws://<robot>:9090`, rws)

| Function   | Topic                         | Payload                                   |
| ---------- | ----------------------------- | ----------------------------------------- |
| Drive      | `/joystick`                   | `{x, y}` in −1..1, `y>0` forward. Latched value re-published every 150 ms while engaged, exactly one `{x:0,y:0}` on release, silence while idle. |
| Head tilt  | `/mars/head/set_position`     | `{data: <int deg>}`, clamped −40..70      |
| Head pos   | `/mars/head/current_position` | JSON-in-String `{current_position, …}`    |
| Speech     | `/brain/tts`                  | `{data: text}`                            |
| Battery    | `/battery_state`              | `sensor_msgs/BatteryState` (0.2 Hz)       |
| Robot info | `/robot/info`                 | JSON-in-String `{robot_name, version, …}` |
| Arm follow | `/leader_positions`           | `Int32MultiArray` of 6 raw Dynamixel ticks (2048 = center); robot converts to `/mars/arm/commands` |
| Video/mic  | `/webrtc/start` → offer on `/webrtc/offer`, answer on `/webrtc/answer`, ICE via `/webrtc/ice_in` / `/webrtc/ice_out` | start payload `{data: '{"source":"live","audio":bool}'}`; the robot rebuilds its pipeline on every start, so toggling audio re-handshakes (debounced, freeze-frame kept) |

Notes:

- **One video consumer at a time.** The robot has a single WebRTC pipeline
  and `/webrtc/offer` is a broadcast topic — every connected client answers
  every offer, so two clients (e.g. the mobile app's camera view and this
  webapp) endlessly steal the stream from each other. Close one before using
  the other. Drive/TTS/telemetry are unaffected.
- **Robot mic** starts muted and only becomes audible from the toggle's click
  (autoplay policy). Chromium/Firefox then allow re-play after rebuilds;
  Safari may need a second click.
- **Keyboard drive**: WASD / arrows, `Shift` = slow. Suppressed while typing
  in the TTS bar. Focus loss (Cmd+Tab) halts the robot immediately.
- If video stalls on a real robot, suspect mDNS ICE obfuscation first
  (browsers mask host candidates as `*.local`); LAN usually still connects
  via the robot's host candidates.

## Leader-arm teleop (USB)

Plug the leader arm into the computer running the browser. The teleop page's
**leader arm** panel reads its six Dynamixel servos directly over WebSerial
(Protocol 2.0 SyncRead at 1 Mbaud, ~50–60 Hz) and, while engaged, streams raw
ticks to `/leader_positions` over the same rosbridge socket — the path the
robot already consumes. No UDP, no robot changes, no drivers beyond the OS
serial driver.

- **Use a secure origin.** Browsers only expose WebSerial there — the robot's
  HTTPS front door (`https://<robot>:4443`, see above), so arm teleop works
  straight from the robot-served page.
- **First time:** click *Connect arm* and pick the USB serial device. The
  grant persists — afterwards the arm attaches automatically, even when
  plugged in mid-session.
- **Engage follow** starts publishing; the follower arm snaps to the leader's
  pose immediately, so hold the leader in a sane position first. Disengaging
  (or hiding the tab, or losing rosbridge) stops new commands and the arm
  holds its last pose.
- Chrome/Edge only (WebSerial). Protocol layer is tested headlessly:
  `node tests/dynamixel.test.js`.

## Nav page

The map widget carries three opt-in overlays, toggled by the header chips:
laser scan (`/scan`), global costmap
(`/navigation/global_costmap/costmap`), and an odometry trail. Toggling a
layer off drops its subscription, so an unused costmap costs no bandwidth.
The **local** costmap is deliberately not overlaid: it lives in the `odom`
frame and would need a live `map->odom` transform to sit on the map canvas.

**Mapping.** The Maps panel ports the mobile app's workflow: list/switch/
delete maps (`/nav/available_maps`, `/nav/change_navigation_map`,
`/nav/delete_map`), a map-free toggle, and **+ New map**, which flips the
robot into mapping mode (`/nav/change_mode {mode: "mapping"}`). While
mapping, a banner over the scene runs the record flow (Finish → name →
Save, mirroring the mobile app's record/name screens); Save calls
`/nav/save_map` (name must be alphanumeric/`_`/`-`; `.yaml` appended
server-side), then returns to navigation and activates the new map — the
same sequence the mobile app runs. All nav state and every mode_manager
call live in `js/nav/navStore.js` (the webapp's port of the mobile app's
MapDataContext); the panel, banner, and page are views of that store. The widget swaps its pose source to
`/mapping_pose` (map-frame TF, published by mode_manager only while
mapping) because raw `/odom` drifts against the growing SLAM map and any
AMCL fix predates it. Mode follows the `/nav/current_mode` topic, so a
mapping session started from the mobile app shows the same controls here.
While mapping, the scene strips down to the growing map + live scan (the
costmap belongs to the previous map; layer chips lock), and the teleop
drive kit mounts over it — main-camera PiP (WebRTC, or the sim viewer in
sim), virtual joystick, WASD, and head tilt — because you drive the robot
to build the map, exactly like the mobile app's record screen.

**Measured velocity is derived, not read.** This robot never populates
`Odometry.twist` — `mars_bringup`'s `_publish_odometry` copies pose out of the
I2C transform and publishes, leaving twist at its zero default (the sim driver
does the same), and no other topic carries measured base motion (every
`/cmd_vel*` is a *command*). So `js/nav/odomVelocity.js` differentiates the raw
odom pose instead. It differentiates *raw* odom, never the map-frame composite:
the odom frame is continuous, whereas a map-frame pose jumps on every AMCL
correction and would read as a velocity spike. Tested headlessly:
`node tests/odomVelocity.test.js`.
