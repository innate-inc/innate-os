# cuVSLAM Integration Research Memo

**Date:** 2026-02-22
**Status:** Draft — Pending Stakeholder Review
**Target Platform:** Jetson Orin Nano 8GB / ROS2 Humble

---

## 1. Executive Summary

This memo evaluates integrating NVIDIA's cuVSLAM (GPU-accelerated visual SLAM) into the innate-os codebase, which currently uses SLAM Toolbox (2D lidar SLAM) for mapping and AMCL for localization. Four architecture options were analyzed by independent researcher, architect, and devil's advocate agents.

**Key findings:**

- cuVSLAM offers GPU-accelerated visual SLAM with stereo cameras, but does **not** produce 2D occupancy grids required by the current Nav2 stack
- The robot already has the sensor hardware cuVSLAM needs (stereo cameras, depth estimation) but lacks an active IMU integration
- **Platform-level blockers exist** that must be resolved before any architecture work: JetPack/Orin Nano support uncertainty, NITROS/Zenoh RMW compatibility, and GPU memory budget on an 8GB shared-memory system
- The recommended path is an incremental approach (Option D: visual odometry only), but even this carries more risk than initially apparent
- A hardware validation sprint (Step 0) is required before committing to any integration path

---

## 2. Current State Analysis

### 2.1 SLAM Toolbox (Mapping Mode)

The robot uses `async_slam_toolbox_node` for map building, lifecycle-managed by `mode_manager.py`.

| Aspect | Detail |
|--------|--------|
| Package | `slam_toolbox` (lifecycle variant) |
| Input | `/scan` (LaserScan, RPLidar throttled to 3 Hz) |
| Output | `map -> odom` TF, `/map` (OccupancyGrid) |
| Solver | Ceres (SPARSE_NORMAL_CHOLESKY, LEVENBERG_MARQUARDT) |
| Resolution | 0.05m (5cm cells) |
| Range | 0.2m - 10.0m |
| Loop closure | Enabled (search within 3.0m) |
| TF publish rate | 50 Hz |
| Map update | Every 0.5s |
| Frames | `odom_frame: odom`, `map_frame: map`, `base_frame: base_footprint` |

**Key files:**
- `ros2_ws/src/maurice_bot/maurice_nav/launch/mapping.launch.py`
- `ros2_ws/src/maurice_bot/maurice_nav/config/mapping_params_init.yaml`

### 2.2 Localization (Navigation Mode)

AMCL provides particle filter localization, seeded by a CuPy GPU-accelerated grid localizer.

**AMCL:**
- 1000-6000 particles, likelihood field laser model
- Subscribes: `/scan` (3 Hz), `/map` (OccupancyGrid)
- Publishes: `map -> odom` TF, `/amcl_pose` (PoseWithCovarianceStamped)
- Config: `ros2_ws/src/maurice_bot/maurice_nav/config/amcl.yaml`

**Grid Localizer:**
- GPU scan matching via CuPy (cupy-cuda12x)
- 4000 candidate positions x 36 angle samples per batch
- Uses `/scan_fast` (raw ~25 Hz) and `/map`
- Publishes initial pose to `/initialpose` for AMCL seeding
- Auto-localizes on startup with 30s timeout
- File: `ros2_ws/src/maurice_bot/maurice_nav/maurice_nav/grid_localizer.py`

### 2.3 Mode Manager

Central orchestrator managing three navigation modes via lifecycle node transitions.

```
modes_nodes = {
    'mapping':    ['slam_toolbox'],
    'navigation': ['navigation_map_server', 'navigation_grid_localizer',
                   'navigation_amcl', 'mapfree/planner_server',
                   'navigation/planner_server', 'controller_server',
                   'bt_navigator', 'behavior_server', 'velocity_smoother'],
    'mapfree':    ['null_map_node', 'navigation/planner_server',
                   'mapfree/planner_server', 'controller_server',
                   'bt_navigator', 'behavior_server', 'velocity_smoother'],
}
```

**Services:**
- `/nav/change_mode` — Switch between mapping/navigation/mapfree
- `/nav/save_map` — Save SLAM map (mapping mode only, uses `map_saver_cli`)
- `/nav/change_navigation_map` — Switch map during navigation
- `/nav/delete_map` — Delete a saved map

**Map management:** PGM+YAML files in `$INNATE_OS_ROOT/maps/`. Discovery via `*.yaml` scan. Current map persisted to `.last_map`. Published to app as JSON on `/nav/available_maps`.

**Known workaround:** `skip_cleanup_nodes` for `controller_server` due to Zenoh RMW crash during TF unsubscription in lifecycle cleanup.

**File:** `ros2_ws/src/maurice_bot/maurice_nav/maurice_nav/mode_manager.py` (~1062 lines)

### 2.4 Nav2 Stack

| Component | Config File | Key Settings |
|-----------|-------------|--------------|
| Global Planner | `planner.yaml` | SmacPlanner2D, 0.6m tolerance |
| Local Controller | `controller.yaml` | MPPI, 20 Hz, 2000 batch, 2.5s horizon |
| Global Costmap | `costmap.yaml` | static_layer (`/map`) + obstacle_layer (`/scan`) + inflation (0.3m) |
| Local Costmap | `costmap.yaml` | SpatioTemporalVoxelLayer (`/mars/main_camera/points`) + obstacle_layer (`/scan`) + inflation (0.25m) |
| Mapfree Costmap | `costmap.yaml` | 10m rolling window, no static_layer |
| Behavior Tree | `nav_to_pose.xml` | PlannerSelector between navigation/mapfree, 1 Hz replanning, backup+clear+wait recovery |
| Velocity Smoother | `velocity_smoother.yaml` | Max vx=0.5 m/s, wz=0.8 rad/s |

**Critical dependency:** The global costmap `static_layer` subscribes to `/map` (OccupancyGrid). Without this, global planning degrades to rolling-window only (equivalent to mapfree quality).

### 2.5 Camera System

| Camera | Resolution | FPS | Format | Topics |
|--------|-----------|-----|--------|--------|
| Main stereo (USB) | 640x480 per eye | 30 Hz | bgr8 | `/mars/main_camera/left/image_raw`, `right/image_raw` |
| Arm (Arducam) | 640x480 | 30 Hz | bgr8 | `/mars/arm/image_raw` |

**Stereo depth:** VPI SGM on CUDA at 8 Hz max. Produces `/mars/main_camera/depth/image_rect_raw` (16SC1, mm) and `/mars/main_camera/points` (PointCloud2, 160x120 decimated).

**Camera info:** Published at 30 Hz with calibration from `stereo_calib.yaml` (K, D, R, T, R1, R2, P1, P2, Q matrices).

**Frames:** `camera_optical_frame` (left), `right_camera_optical_frame` (right).

**File:** `ros2_ws/src/maurice_bot/maurice_cam/src/main_camera_driver.cpp`

### 2.6 TF Tree

```
map
  └── [SLAM Toolbox or AMCL] ──> odom
        └── [I2C wheel odom @30Hz] ──> base_link
              ├── [static] ──> base_footprint
              ├── [static] ──> base_laser (-0.0764, 0, 0.17165)
              ├── [URDF joints] ──> link1 -> ... -> link6 -> ee_link (arm)
              ├── [URDF joints] ──> head -> head_camera_left, head_camera_right
              ├── [URDF fixed] ──> camera_optical_frame, right_camera_optical_frame
              └── [URDF fixed] ──> arm_camera_link
```

**Odometry sources:**
- Primary: I2C bus (address 0x42, 30 Hz) — MCU sends X, Y, theta
- Secondary: UART (`/dev/ttyTHS1`, 115200 baud, 50 Hz) — 10-byte packets
- Simulation: MuJoCo qpos direct read (500 Hz sim, 30 Hz publish)

**IMU:** Frame defined in URDF (`imu_link`) but **no active IMU data integration exists** in the codebase.

### 2.7 Brain Client Dependencies

The brain client (`brain_client_node.py`, ~3800 lines) has direct dependencies on navigation state:

- Subscribes to `/amcl_pose` and uses **covariance values** (`cov_x`, `cov_y`, `cov_yaw`) to communicate localization uncertainty to the cloud vision agent
- Uses TF `map -> base_link` lookup for robot pose
- Publishes mode/map state to the app via WebSocket

### 2.8 Docker & Build System

- Base image: `ros:humble-ros-base` (Ubuntu 22.04)
- CUDA 12.6, VPI 3 (hardware mode)
- CuPy GPU arrays for grid localizer
- `MODE=simulation|hardware` build arg
- No Isaac ROS packages currently installed
- RMW: Zenoh (`rmw_zenoh_cpp`)
- Production: `docker-compose.prod.yml` with `network_mode: host`, `privileged: true`

---

## 3. cuVSLAM Technology Assessment

### 3.1 Overview

cuVSLAM is NVIDIA's GPU-accelerated visual SLAM library providing stereo-visual-inertial SLAM and odometry. It processes stereo camera images on the GPU, performing simultaneous localization and mapping.

**ROS2 package:** `isaac_ros_visual_slam`

### 3.2 ROS2 API

**Subscribed topics (inputs):**

| Topic | Type | Description |
|-------|------|-------------|
| `visual_slam/image_{i}` | `sensor_msgs/Image` | Grayscale image from camera i |
| `visual_slam/camera_info_{i}` | `sensor_msgs/CameraInfo` | Camera intrinsics |
| `visual_slam/imu` | `sensor_msgs/Imu` | IMU (tracking_mode=1 only) |
| `visual_slam/depth_0` | `sensor_msgs/Image` | Depth map (tracking_mode=2 / RGBD) |

**Published topics (outputs):**

| Topic | Type | Description |
|-------|------|-------------|
| `visual_slam/tracking/odometry` | `nav_msgs/Odometry` | Primary odometry output |
| `visual_slam/tracking/vo_pose_covariance` | `geometry_msgs/PoseWithCovarianceStamped` | Pose with uncertainty |
| `visual_slam/tracking/vo_pose` | `geometry_msgs/PoseStamped` | Current pose |
| `visual_slam/tracking/slam_path` | `nav_msgs/Path` | SLAM-corrected trail |
| `visual_slam/status` | `VisualSlamStatus` | Diagnostic (vo_state: 0=Unknown, 1=Success, 2=Failed) |

**Services:**

| Service | Interface | Description |
|---------|-----------|-------------|
| `visual_slam/save_map` | `FilePath` | Save visual landmark map |
| `visual_slam/load_map` | `FilePath` | Load visual landmark map |
| `visual_slam/localize_in_map` | `LocalizeInMap` | Relocalize in saved map |
| `visual_slam/reset` | `Reset` | Reset tracking |
| `visual_slam/set_slam_pose` | `SetSlamPose` | Set current pose |

**TF published:**
- `map -> odom` (configurable via `publish_map_to_odom_tf`)
- `odom -> base_link` (configurable via `publish_odom_to_base_tf`)

### 3.3 Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `num_cameras` | 2 | Number of cameras (max 32) |
| `tracking_mode` | 0 | 0=Multi-cam, 1=VIO, 2=RGBD |
| `enable_localization_n_mapping` | true | SLAM vs VO-only |
| `rectified_images` | true | Pre-rectified input |
| `map_frame` / `odom_frame` / `base_frame` | map / odom / base_link | TF frames |
| `publish_map_to_odom_tf` | true | Broadcast map->odom |
| `publish_odom_to_base_tf` | true | Broadcast odom->base |
| `sync_matching_threshold_ms` | 5.0 | Max stereo sync delta |
| `image_jitter_threshold_ms` | 34.0 | Acceptable frame jitter |
| `slam_max_map_size` | 300 | Max poses in SLAM map |

### 3.4 Performance

- Translation error: 0.94% on KITTI benchmark
- Runtime: 0.007 s/frame on Jetson AGX Xavier
- Camera requirements: 30+ Hz, ±2ms jitter, ±100us stereo sync

### 3.5 Critical Limitations

1. **Does NOT produce 2D occupancy grids** — only 3D visual landmark maps. Needs nvblox for Nav2 costmap compatibility.
2. **Jetson Orin Nano support uncertain** — latest docs (JetPack 7.1) only list Jetson Thor. Prior JetPack 6 releases supported Orin Nano, but current status is unclear.
3. **Closed-source backend** — cannot debug or modify the SLAM algorithm.
4. **No wheel odometry fusion** — purely visual/inertial.
5. **NITROS dependency** — uses NVIDIA's custom GPU-accelerated transport layer. Compatibility with Zenoh RMW undocumented.
6. **IMU fallback is brief** — only ~1s of tracking without visual features via IMU, ~0.5s constant-velocity estimation after that.
7. **Camera sync requirements strict** — ±2ms jitter, ±100us stereo sync.

### 3.6 nvblox (for Nav2 Costmap Compatibility)

nvblox converts depth images + cuVSLAM pose into a 3D TSDF voxel grid, then exports 2D occupancy grid slices for Nav2.

- Subscribes: depth images, cuVSLAM pose/TF
- Publishes: 2D costmap slices (`DistanceMapSlice`)
- `nvblox_nav2` package provides Nav2 costmap layer plugin
- Significant additional GPU memory requirements (~1-2 GB)

---

## 4. Integration Architecture Options

### Option A: Full Replacement

Replace SLAM Toolbox + AMCL entirely with cuVSLAM.

```
Stereo Camera (30Hz) ──> cuVSLAM (GPU) ──> map->odom TF + odometry
RPLidar (/scan) ─────────────────────────> costmap obstacle_layer only

                     ⚠ NO 2D occupancy grid for Nav2 global costmap
```

| Dimension | Assessment |
|-----------|------------|
| Risk | **VERY HIGH** — no 2D occupancy grid breaks SmacPlanner2D global planning |
| Reversibility | Low — existing PGM maps become useless |
| Simulation | Broken — cuVSLAM needs real cameras |
| Complexity | High — complete mapping/nav pipeline rewrite |
| Map format | Binary visual landmarks (opaque, not human-readable) |

**Verdict: Not recommended.** Fundamental architectural mismatch with Nav2 global planning.

### Option B: Parallel/Hybrid

Add `visual_mapping` and `visual_navigation` as new modes alongside existing lidar-based modes.

```
mapping (existing)          ──> SLAM Toolbox ──> PGM/YAML maps
visual_mapping (NEW)        ──> cuVSLAM ──> visual landmark maps
navigation (existing)       ──> map_server + AMCL ──> Nav2
visual_navigation (NEW)     ──> cuVSLAM localize ──> Nav2 (no static layer)
mapfree (existing)          ──> unchanged
```

| Dimension | Assessment |
|-----------|------------|
| Risk | **Medium-High** — existing modes untouched, but `visual_navigation` still lacks static layer. Maintaining 5 modes instead of 3. |
| Reversibility | High — remove new mode entries to revert |
| Simulation | Minimal — lidar modes still work in sim |
| Complexity | Moderate — additive mode_manager changes |
| Map format | Dual: PGM/YAML (lidar) + binary (visual) |

**Verdict: Good middle ground** after Option D is proven, but `visual_navigation` quality is no better than mapfree without nvblox.

### Option C: cuVSLAM + nvblox

Full visual SLAM pipeline with nvblox generating 2D costmaps for Nav2 compatibility.

```
Stereo Camera ──> cuVSLAM ──> map->odom TF + pose
                    │
Depth Images  ──> nvblox (TSDF) ──> 2D occupancy grid ──> Nav2 static_layer
```

| Dimension | Assessment |
|-----------|------------|
| Risk | **High** — GPU memory on Orin Nano 8GB is likely insufficient (estimated 6-7 GB total). Two closed-source dependencies with JetPack version coupling. |
| Reversibility | Low-Moderate |
| Simulation | Broken — needs camera-capable sim (Isaac Sim) |
| Complexity | Very High — two major GPU dependencies, TSDF tuning |
| Map format | cuVSLAM binary + nvblox mesh + exportable PGM |

**Verdict: Theoretical ideal, but likely infeasible on Orin Nano 8GB.** Could be the long-term goal if hardware upgrades.

### Option D: Incremental (Visual Odometry Only)

cuVSLAM provides visual odometry, fused with wheel odometry via `robot_localization` EKF. All existing SLAM/Nav2 infrastructure preserved.

```
Stereo Camera ──> cuVSLAM (VO mode) ──> /visual_odom
                                              │
Wheel Encoders ──> /odom                      │
                      │                       │
                      └───> robot_localization EKF ──> odom->base_link TF (fused)
                                                            │
RPLidar ──> SLAM Toolbox (mapping) ──> map->odom TF         │
         ──> AMCL (navigation) ──> map->odom TF             │
                                                             │
                                   Nav2 stack (UNCHANGED) <──┘
```

| Dimension | Assessment |
|-----------|------------|
| Risk | **Medium** (not "Low" as initially assessed — see Section 5) |
| Reversibility | Very High — remove cuVSLAM+EKF, re-enable wheel odom TF |
| Simulation | Minimal — cuVSLAM skipped in sim, EKF uses wheel-only |
| Complexity | Low-Moderate — 3 new files, minor mode_manager edits |
| Map format | Unchanged (PGM/YAML) |

**Benefits:**
- Better odometry reduces drift, improving SLAM Toolbox map quality
- Better AMCL convergence from smoother odom input
- Smoother MPPI control from lower-noise pose estimates
- Wheel slip detection via visual/wheel odom disagreement
- Graceful degradation to wheel-only if cuVSLAM fails

**Verdict: Recommended starting point**, with caveats detailed in Section 5.

### Comparative Summary

```
                  Option A    Option B    Option C    Option D
                  Full Rplc   Parallel    +nvblox     VO Only
                  ─────────   ────────    ───────     ───────
2D Map Compat       No         Yes(lidar)  Yes(nvblx)  Yes
Nav2 Static Layer   No         Yes(lidar)  Yes         Yes
Existing Maps       Lost       Kept        Lost/Cvt    Kept
Sim Compat          No         Yes(lidar)  No          Yes
GPU Memory         ~1 GB       ~1 GB       ~3-4 GB     ~0.5-1 GB
Complexity          High        Moderate    Very High   Low-Mod
Risk               Very High   Med-High    High        Medium
Reversibility       Low         High        Low-Med     Very High
Odom Quality       Better      Both        Better      Better
Mapping Quality    Visual only  Both        Best        Better
```

---

## 5. Critical Assessment and Risks

### 5.1 Platform-Level Blockers (Must Resolve Before Any Work)

#### CRITICAL: JetPack / Orin Nano Support Uncertainty

cuVSLAM latest documentation (JetPack 7.1) only lists Jetson Thor as a supported platform. Orin Nano was supported in prior JetPack 6 / Isaac ROS 3.x releases, but current status is unclear. If cuVSLAM is no longer supported on Orin Nano, the entire effort is moot.

**Action required:** Verify cuVSLAM binary availability for Orin Nano with current JetPack before any architecture work.

#### CRITICAL: GPU Memory Budget

Orin Nano has 8GB **unified** (shared CPU+GPU) memory. Current GPU consumers:

| Component | Estimated GPU Memory | Duty Cycle |
|-----------|---------------------|------------|
| VPI Stereo SGM | ~150-200 MB | Continuous @ 8Hz |
| CuPy Grid Localizer | ~200-500 MB | Intermittent |
| PyTorch ACT inference | ~500 MB - 1 GB | During manipulation |
| CUDA runtime | ~200 MB | Always |
| Linux OS + ROS2 | ~1-2 GB | Always |
| **Current subtotal** | **~2-4 GB** | |
| + cuVSLAM (Option D) | ~500 MB - 1 GB | Continuous |
| + nvblox (Option C) | ~1-2 GB | Continuous |

Option D total: ~3-5 GB — feasible but tight, especially during ACT inference.
Option C total: ~5-7 GB — **OOM risk is real**, particularly when `torch.compile()` triggers kernel compilation warmup.

**Action required:** Profile actual GPU memory on hardware before integration.

#### HIGH: NITROS + Zenoh RMW Compatibility

cuVSLAM uses NVIDIA's NITROS (GPU-accelerated transport). The current system uses Zenoh RMW. Compatibility between NITROS and Zenoh is undocumented. Isaac ROS typically uses Cyclone DDS or Fast-DDS. **cuVSLAM may not function at all with Zenoh RMW.**

**Action required:** Test NITROS node communication under Zenoh RMW on target hardware.

### 5.2 Technical Risks

#### HIGH: Docker Base Image Mismatch

Isaac ROS / cuVSLAM packages typically require NVIDIA's L4T-based container images (`nvcr.io/nvidia/isaac/ros:*`), not generic `ros:humble-ros-base`. Options:
- Rebase entire Docker image on NVIDIA's container (breaking change)
- Install Isaac ROS packages into current container (version conflicts likely)
- Run cuVSLAM in a separate container with inter-container ROS communication

None are trivial. This is unaddressed in the architecture options.

#### HIGH: robot_localization EKF Complexity (Option D)

The EKF fusion in Option D is presented as straightforward. In reality:
- `robot_localization` is not in the codebase — it's a new dependency
- Process noise (`Q`) and measurement noise (`R`) covariance matrices require empirical tuning on the actual robot
- cuVSLAM's covariance accuracy is undocumented (closed-source)
- Incorrect tuning produces **worse** odometry than either source alone

#### HIGH: No Active IMU

cuVSLAM's VIO mode (`tracking_mode=1`) requires calibrated IMU data. The robot has an IMU frame in URDF but zero active IMU integration. This limits cuVSLAM to `tracking_mode=0` (multi-camera VO only), with **no fallback** when visual tracking fails (texture-poor areas, rapid motion, occlusion).

#### HIGH: Brain Client AMCL Pose Dependency

`brain_client_node.py` subscribes to `/amcl_pose` and forwards covariance values (`cov_x`, `cov_y`, `cov_yaw`) to the cloud vision agent. cuVSLAM does not publish to `/amcl_pose`. Even with topic remapping, the covariance semantics differ — AMCL covariance represents particle filter spread, while cuVSLAM covariance semantics are undocumented. Wrong covariance values lead to wrong agent decisions.

#### HIGH: Camera Timestamp Synchronization

cuVSLAM requires ±100us stereo sync and ±2ms inter-frame jitter. The current pipeline uses GStreamer software timestamps. Whether this meets cuVSLAM's requirements is unknown and must be measured on hardware.

#### HIGH: "Graceful Degradation" is Undefined

Option D claims cuVSLAM failure results in graceful fallback to wheel-only odom. But:
- How is failure detected? cuVSLAM may publish increasingly wrong poses before failing outright
- No localization health monitoring infrastructure exists in the codebase
- The behavior tree has no recovery actions for localization failure

#### HIGH: No Testing/Validation Infrastructure

No SLAM quality metrics exist in the codebase. Validating that cuVSLAM improves odometry requires:
- Ground truth trajectory data (motion capture or high-accuracy reference)
- Metrics: ATE (Absolute Trajectory Error), RPE (Relative Pose Error)
- Automated regression testing

### 5.3 Medium-Severity Concerns

| # | Concern | Details |
|---|---------|---------|
| 1 | Zenoh RMW lifecycle bug | cuVSLAM publishes TF transforms; lifecycle transitions may trigger the known Zenoh crash during TF unsubscription |
| 2 | BT recovery gap | No behavior tree nodes for localization failure recovery; visual SLAM is less robust than lidar AMCL |
| 3 | Map management UI coupling | App expects named YAML/PGM maps; cuVSLAM binary maps require UX redesign (Options A/B/C) |
| 4 | Grid localizer necessity | In Option D, grid localizer is still needed for AMCL seeding after power cycles; cannot be removed to save GPU memory |
| 5 | Simulation gap | cuVSLAM development/testing requires physical hardware; MuJoCo/Stage simulators lack visual features |
| 6 | Option B risk underrated | Maintaining 5 navigation modes (3 existing + 2 visual) increases testing matrix and maintenance burden |

---

## 6. Recommendation Summary

### Step 0: Hardware Validation (MUST DO FIRST)

Before committing to any architecture:

1. **Confirm cuVSLAM availability** for Orin Nano / JetPack 6.x
2. **Test NITROS + Zenoh RMW** compatibility
3. **Measure GPU memory** of cuVSLAM alone on target hardware
4. **Test camera pipeline** timestamp synchronization against cuVSLAM requirements
5. **Profile combined GPU load** (VPI stereo + cuVSLAM + CuPy) on Orin Nano 8GB

If any of these fail, the effort should be paused until the blocker is resolved (e.g., hardware upgrade, JetPack update, IMU integration).

### If Step 0 Passes: Phased Approach

**Phase 1 — Observation Mode (Option D, reduced scope)**
- Run cuVSLAM as a logging-only VO source — publish `/visual_odom` but do NOT feed into control loop
- Build localization health monitoring (feature count, tracking state, pose jump detection)
- Collect comparison data: cuVSLAM VO vs wheel odom over extended operation
- Validate VO quality before proceeding

**Phase 2 — EKF Fusion (Option D, full scope)**
- Integrate `robot_localization` EKF
- Empirical covariance tuning with collected data
- A/B testing: fused odom vs wheel-only, measuring SLAM Toolbox map quality and AMCL convergence
- Define and implement graceful degradation (cuVSLAM health watchdog)

**Phase 3 — Visual Mapping Mode (Option B, partial)**
- Add `visual_mapping` mode to mode_manager
- Enable cuVSLAM landmark map saving/loading
- Validate visual map quality in target environments
- Do NOT add `visual_navigation` without solving the static layer gap

**Phase 4 — Full Visual Navigation (Option C)**
- Only if hardware supports it (likely requires Orin NX 16GB or better)
- Integrate nvblox for 2D costmap generation
- Replace AMCL with cuVSLAM relocalization
- Full Nav2 stack compatibility via nvblox costmap layer

---

## 7. Open Questions for Stakeholder Decision

1. **Hardware commitment:** Is the Jetson Orin Nano 8GB the long-term target platform, or is an upgrade to Orin NX 16GB on the roadmap? This fundamentally affects which options are feasible (Option C requires more GPU memory than Orin Nano likely has).

2. **IMU activation:** Is there a plan to integrate the existing IMU hardware? cuVSLAM's VIO mode (`tracking_mode=1`) significantly improves robustness but requires active IMU data. Without it, cuVSLAM has no fallback when visual tracking fails.

3. **Simulation strategy:** cuVSLAM cannot run in the current Stage/MuJoCo simulation. Is the team willing to invest in camera-capable simulation (Gazebo, Isaac Sim) for visual SLAM development, or accept that this work is hardware-only?

4. **Risk tolerance for Step 0:** Are we prepared to invest engineering time in hardware validation that may conclude cuVSLAM is not viable on current hardware? What is the fallback plan?

5. **Map format migration:** If visual SLAM modes are added (Options B/C), existing PGM/YAML maps cannot be used by cuVSLAM. Is re-mapping all environments acceptable? Do customers have expectations about map portability?

6. **JetPack upgrade path:** The current system uses JetPack 6.x. cuVSLAM's latest version targets JetPack 7.1. Is a JetPack upgrade planned? What other system components would be affected?

7. **Priority vs. other work:** Given the blockers and risks, is cuVSLAM integration the highest-value use of engineering time, or would activating the IMU, improving stereo depth quality, or enhancing the existing SLAM Toolbox configuration yield better near-term results?

---

*This memo was prepared by a research team of autonomous agents: a codebase researcher, a cuVSLAM technology researcher, a systems architect, and a devil's advocate critic. All claims about the codebase were verified against source files.*
