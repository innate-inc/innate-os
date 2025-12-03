# Spline Path Viewer + Recorder

Interactive tool for drawing spline paths on a SLAM map, executing them on the robot, and recording synchronized camera/odometry data.

## Architecture

```
┌─────────────────────────────┐         ┌──────────────────────────────────┐
│   Your PC                   │         │   Robot (Jetson)                 │
│                             │   SSH   │                                  │
│  spline_viewer.html         │◄───────►│  spline_path_server              │
│  (Browser)                  │  :8770  │         │                        │
│       │                     │         │         ▼                        │
│       │                     │         │  camera_odom_recorder            │
│       ▼                     │         │         │                        │
│  Downloads .h5 + metadata   │◄────────│         ▼                        │
│                             │         │  FollowPath Action (MPPI)        │
└─────────────────────────────┘         └──────────────────────────────────┘
```

## Features

- **Interactive spline drawing**: Click to add control points, visualize interpolated path
- **Synchronized recording**: Camera frames + odometry + head position recorded during trajectory
- **Automatic data transfer**: Recording is transferred to PC and saved as `.h5` + `metadata.json`
- **Task labeling**: Label each trajectory for organized data collection

## Usage

### 1. On the Robot

Build the workspace (if needed):
```bash
cd ~/innate-os/ros2_ws
colcon build --packages-select manipulation
source install/setup.bash
```

Start the navigation stack (if not already running):
```bash
# Mode manager auto-starts navigation, or manually:
ros2 launch maurice_nav navigation.launch.py
```

Start the spline path server (includes camera recorder):
```bash
ros2 launch manipulation spline_path_server.launch.py
```

### 2. On Your PC

Port forward from your PC to the robot:
```bash
ssh -L 8770:localhost:8770 jetson1@<robot_ip>
# Or use a different local port if 8770 is in use:
ssh -L 8771:localhost:8770 jetson1@<robot_ip>
```

Copy `spline_viewer.html` to your PC and open it in a browser:
```bash
scp jetson1@<robot_ip>:~/innate-os/tools/spline_viewer/spline_viewer.html .

# Open in browser (just double-click or serve it)
python3 -m http.server 8000
# Then navigate to http://localhost:8000/spline_viewer.html
```

### 3. Using the Viewer

1. **Connect**: Enter the WebSocket URL (e.g., `ws://localhost:8771`) and click "Connect"
2. **View Map**: The SLAM map loads automatically
3. **Enter Task Label**: Describe the task (e.g., "navigate_to_kitchen", "approach_table")
4. **Draw Path**: Click on the map to add control points
   - Green dot = start point
   - Blue dots = intermediate points  
   - Red dot = end point
5. **Execute**: Click "▶ Record & Run"
   - Recording starts automatically
   - Robot follows the spline path
   - Recording stops when path completes
   - Data is transferred to your PC and downloaded

### Controls

- **Left Click**: Add control point
- **Right Click / Shift+Drag**: Pan the view
- **Mouse Wheel**: Zoom in/out
- **+/-**: Zoom buttons
- **⟲**: Reset view to fit map

## Output Files

Each trajectory execution produces two files:

**`{task_label}_{timestamp}.h5`** - HDF5 recording with:
- `/images/{camera}/data` - Compressed JPEG frames
- `/timestamps/images/{camera}` - Frame timestamps
- `/odometry/position`, `orientation`, `linear_velocity`, etc.
- `/timestamps/odometry` - Odometry timestamps
- `/head/position` (optional) - Head servo position
- `/metadata` - Session info

**`{task_label}_{timestamp}_metadata.json`**:
```json
{
  "session_name": "spline_traj_20251203_142500",
  "data_frequency": 10,
  "start_time": "2025-12-03T14:25:00",
  "duration_sec": 15.2,
  "num_frames": 152,
  "camera_topics": ["/mars/main_camera/image/compressed"],
  "odom_topic": "/odom",
  "head_position_topic": "/mars/head/current_position",
  "streaming_mode": "disk",
  "task_label": "navigate_to_door",
  "action_label": "spline_trajectory",
  "source_session": "spline_traj_20251203_142500",
  "source_frames": "0-152"
}
```

## Parameters

### Spline Path Server

| Parameter | Default | Description |
|-----------|---------|-------------|
| `websocket_port` | 8770 | WebSocket server port |
| `use_amcl_pose` | true | Use AMCL for robot pose |
| `map_topic` | /map | OccupancyGrid topic |
| `odom_topic` | /odom | Odometry topic |
| `amcl_pose_topic` | /amcl_pose | AMCL pose topic |
| `recording_data_dir` | ~/innate-os/camera_odom_recordings | Recording directory |

### Camera Odom Recorder

| Parameter | Default | Description |
|-----------|---------|-------------|
| `data_directory` | ~/innate-os/camera_odom_recordings | Recording directory |
| `data_frequency` | 10 | Recording frequency (Hz) |
| `camera_topics` | ['/mars/main_camera/image/compressed'] | Camera topics |
| `head_position_topic` | /mars/head/current_position | Head position topic |

## Workflow

1. **Start recording** → Calls `/calibrate` service, waits for odometry
2. **Wait 0.2s** → Ensures clean recording start
3. **Execute path** → MPPI controller follows the spline with obstacle avoidance
4. **Wait 0.2s** → Ensures clean recording end
5. **Stop recording** → HDF5 file finalized on robot
6. **Transfer** → Server reads file, compresses, sends via WebSocket
7. **Save** → Browser downloads `.h5` and `metadata.json` files

## Troubleshooting

**"FollowPath action server not available"**
- Make sure navigation is running: `ros2 launch maurice_nav navigation.launch.py`

**"Failed to start recording"**
- Check camera_odom_recorder is running: `ros2 node list | grep camera_odom`
- Check services: `ros2 service list | grep camera_odom`

**Map not loading**
- Verify map is published: `ros2 topic echo /map --once`

**Robot not moving**
- Check AMCL localization: `ros2 topic echo /amcl_pose`
- Verify transforms: `ros2 run tf2_ros tf2_echo map base_link`

**Recording transfer fails**
- Check robot disk space
- Check recording directory exists and is writable

## Dependencies

Server (Robot):
- ROS2 (tested on Humble)
- nav2_msgs
- websockets (`pip install websockets`)
- h5py

Client (Browser):
- Modern browser with WebSocket support
- pako.js (loaded from CDN for zlib decompression)
