# Getting real WebRTC FEC: the GStreamer upgrade question

## Why this document exists

Field measurements (2026-08-18, remote viewer at 240 ms RTT against mars-the-47th, office
uplink dropping ~6% of outbound UDP at every rate — see PR #664) established the hard
limits of our loss-repair stack on GStreamer 1.20:

- NACK/RTX works and repairs losses — but libwebrtc abandons an incomplete delta frame
  after ~200 ms, so on any path with RTT above that, retransmission mathematically cannot
  repair in time. Chrome then renders on broken references (full-frame corruption) until
  a keyframe.
- The only repair with no deadline is in-band FEC. Our transceivers negotiate ULPFEC/RED
  (both offer and Chrome's answer carry `red`/`ulpfec`), **but 1.20's webrtcbin never
  emits a single RED packet** — verified on the wire, including setting `fec-type` /
  `fec-percentage` at transceiver creation (`on-new-transceiver`). The sender-side FEC
  path in 1.20 does not engage for our bundled, appsrc-fed pre-payloaded RTP.

The RTCP-driven sender adaptation (DEGRADED mode: 60% bitrate + 1 Hz keyframes) bounds
the damage, but bounded-ugly is the ceiling until FEC actually flows.

## What newer GStreamer buys

- **webrtcbin fixes**: the WebRTC stack (webrtcbin, RTX/FEC wiring, stats) received
  continuous fixes through 1.22/1.24/1.26; sender FEC and bundle handling are among the
  areas reworked. Our code is already correct — `fec-type=ulp-red` + `fec-percentage`
  set at transceiver creation — so FEC should engage on a fixed runtime with **zero
  application changes** (verify item V2 below is the proof).
- **`webrtcsink` + `rtpgccbwe`** (gst-plugins-rs, needs ≥1.22): the maintained
  "batteries-included" WebRTC sender with Google Congestion Control — real adaptive
  bitrate driven by the receiver, the piece our RTCP ladder approximates coarsely.
  Migrating mars_cam's custom fan-out to webrtcsink would be a larger redesign; the
  incremental win is webrtcbin FEC first, webrtcsink as a later option.

## Upgrade paths (Jetson Orin, L4T = Ubuntu 22.04, system gst 1.20.3)

| Path | Effort | Risk | Notes |
|---|---|---|---|
| **A. JetPack/L4T update** | waiting | low | NVIDIA ships gst with L4T; no current L4T provides ≥1.22. Not in our control; track releases. |
| **B. Newer gst from source into `/opt/gst`** | ~1 day + per-robot install | medium | Build core+base+good+bad (meson, ~30-60 min on Orin or cross/CI). Run only the camera container/process with `GST_PLUGIN_PATH`/`LD_LIBRARY_PATH` pointing at `/opt/gst`, system untouched. NVIDIA's plugins (nvv4l2decoder, nvvidconv, nvjpegenc) are built against 1.20 but the gst plugin ABI is stable within 1.x — community reports have them loading on newer cores; **not NVIDIA-certified**, must be verified per V1. |
| **C. Third-party apt (e.g. savoury1 PPA, 1.24 for 22.04)** | hours | higher | Replaces system gst for *everything* on the robot, not just mars_cam; harder rollback; PPA trust/arm64 coverage to confirm. Path B is strictly safer. |

**Executed: Path B, piloted on mars-the-47th (2026-08-18).** All verification gates
passed (V1-V3 below; V4 soak ongoing), and on a 240 ms / 4-5%-loss path the result was
frozen time 17-29 s/min -> 0.5 s and PLIs -> 0 with FEC at 100%.

## Fleet rollout (the executed pipeline)

1. `scripts/update/build_gst_opt.sh` (this repo) builds the deb natively on any Jetson:
   meson/ninja build into a DESTDIR, runtime-only prune, strip + RUNPATH pinning,
   element + NVIDIA-plugin verification, dpkg-shlibdeps Depends (symbols-file
   version floors), then
   `dpkg-deb -Zxz` -> `innate-gstreamer-opt_<v>-<N>jammy_arm64.deb`. Bump `DEB_INC`
   on any rebuild of the same upstream version or apt never sees an upgrade.
2. Ship it via **innate-packages**: upload the deb as a GitHub Release asset and
   point the `innate-gstreamer-opt` line of `prebuilt-debs.txt` at it (the build
   script prints the exact commands). CI gates the manifest's debs with
   `check-prebuilt-deb.sh --install`, and `publish.sh` fetches, signs and indexes
   them.
3. `ros2_ws/apt-dependencies.hardware.txt` lists `innate-gstreamer-opt`; the normal
   `innate update apply` (post_update.sh) installs it fleet-wide.
4. `camera_composable.launch.py` activates `/opt/gst` automatically when present.

**Ordering**: the deb must be published in innate-packages BEFORE a release containing
the dependency line ships, or post_update's apt step fails on the missing package.

## Verification checklist for the pilot

- **V1 — hardware pipeline intact**: main/arm cameras stream; `nvv4l2decoder`,
  `nvvidconv`, `nvjpegenc` load under the new core (`gst-inspect-1.0` from /opt/gst env);
  video recorder still produces playable MP4s; CPU within current envelope.
- **V2 — FEC on the wire (the point of it all)**: a Chrome viewer's
  `getStats()` inbound-rtp shows `fecPacketsReceived > 0` and the codec list contains
  `red`/`ulpfec`. Then, on a lossy path: PLI/min and freeze counts drop vs 1.20 under the
  same conditions (the profiling HUD now shows both).
- **V3 — LAN latency unchanged**: playout-delay/jitter behavior identical on the office
  network (the adaptive client pins 0 there).
- **V4 — soak**: multi-hour stream + a dozen connect/disconnect cycles; no leaks
  (webrtcbin teardown paths changed between versions — our Peer RAII assumptions must
  hold).

## Rollback

Unset the env override and restart — the system 1.20 stack is untouched by Path B.

## Related

- PR #664 (`theo/webrtc-loss-resilience`) — the repair/adaptation stack this unlocks.
- `docs/REMOTE_TELEOP_DESIGN.md` — TURN/relay design; complementary (helps NAT/QoS, does
  not remove path loss or RTT).
