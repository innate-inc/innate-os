# Household Orders: grounded conversation approach (candidate B)

Prepared September 6, 2026 on `codex/household-conversation-approach`, based on
candidate A `07b24e76c028c378daab81cfdd07d713d0a1a16f`. The hypothesis is that
approaching visible floor at a resident's feet before greeting can reduce distant
greetings and silence retries. No completion-rate, latency, cost, or performance
improvement is claimed: no paid-provider challenge measurement has run for B.

## Scope

`go_to_point_in_view` accepts finite numeric `standoff_m` in [0.35, 1.5]. Omitted
or explicit 0.35 preserves legacy object projection, goals, and outcomes. Larger
standoffs use the existing calibrated pinhole/URDF geometry with the current
main-camera frame's captured pitch; the household prompt requests 1.2 m.

The projected target is rebased from capture pose to current pose **before**
computing standoff, travel cap, and facing. This prevents motion during model
latency from causing a return to the old pose. Missing poses reject conversation
approach. Inside the requested radius, translation is zero; a bearing within
five degrees returns already positioned, otherwise navigation only corrects
heading. Static-map conversion uses that same current pose, without a second
capture delta. Far steps are capped and require a fresh observation for retry.

The prompt allows one initial approach, counts any pre-identity approach toward
that limit, re-identifies the same encounter after moving, and prevents
NOTE_MISSING from restarting the approach loop. One grounded silence retry
replaces the blind one-meter advance. The ten-second reply window, incoming
speech gate, explicit Stop, provider outcomes, identity/notes, original 900-second
challenge, and NPC speech/eligibility remain unchanged. No actor coordinates,
private expected orders, or judge telemetry are inputs to this policy.

## Verification and limitations

- Host: **110 passed, 6 ROS-dependent skipped**, local brain and incoming speech.
  Tests cover default compatibility, input validation, calibrated projection,
  travel bounds, no-op/facing-only behavior, and capture/current pose changes in
  static-map and mapfree modes. Ruff and `git diff --check` passed.
- Coordinator's dedicated ROS test container: **160 passed in 4.20 seconds**
  across local brain, incoming speech, runner deactivation, TTS, and OpenAI
  context. B's package was copied into the test container with ROS/prebuilt
  interfaces; providers were mocked, no ROS nodes started, and the container
  was stopped afterward. Evidence:
  `/Users/axelpeytavin/Documents/Codex/2026-09-05/household-orders-research/evidence/candidate-b-ros-tests.log`.
- Independent medium review passed the five implementation/test files at the
  hashes below. This document was added afterward; reviewed source was frozen.

This verifies deterministic integration with mocked providers, not simulator
completion or physical motion. Visible floor/feet selection is model policy,
not a semantic runtime detector: runtime validates frame dimensions, pose, and
ray geometry. Calibration assumes the existing full-image resize, not a crop.
A's frozen checkout and live measurement/runtime were untouched by B preparation.

## Reviewed SHA-256 provenance

```text
12b6149ed3f89f9392714efffbf4a459c5818ebd106276abb2bb62a4eb1eb5d5  ros2_ws/src/brain/brain_client/brain_client/brain/agent.py
8fab36df13dc256b7b6b759dcb3ce273a73ef328934927a505801764b4aaa2ba  ros2_ws/src/brain/brain_client/brain_client/brain/grounding.py
7525be05cedfefca5344f4e670844451c40d1683ecd5030b4a7d50dd238dd52c  ros2_ws/src/brain/brain_client/brain_client/brain/tools.py
cf14eb397e9971017e1c2f17bd1a24f3119a322da1484a10a172528c5553a6aa  ros2_ws/src/brain/brain_client/test/test_local_brain.py
b6f8eef0738a1fd2c4e98a0fcd74bba9255b53ed7dc02de76ffd3d960958db3f  workspace/innate_agents/household_orders_agent.py
```
