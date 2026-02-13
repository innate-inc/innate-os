#!/usr/bin/env python3
"""
PID Hot-Reload Watcher with Real-Time Joint State Plot.

Watches pid_tuning.yaml for changes and applies new PID/profile values
to the running arm node in real-time. Also plots joint positions as a
live ASCII chart in the terminal (requires: pip install plotext).

Usage:
    python3 pid_hot_reload.py [path/to/pid_tuning.yaml] [--no-plot]
"""

import os
import sys
import time
import json
import yaml
import subprocess
import threading
from collections import deque

# Optional: real-time ASCII plotting
try:
    import plotext as plt
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False

# Optional: ROS2 subscription for joint state data
try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import JointState
    HAS_ROS = True
except ImportError:
    HAS_ROS = False

# --- Configuration ---
HISTORY_SECONDS = 5
HISTORY_SIZE = 500          # samples to keep (~5s at 100 Hz)
PLOT_REFRESH_HZ = 4         # plot redraws per second
JOINT_NAMES = ["J1 Base", "J2 Shoulder", "J3 Elbow", "J4 Wrist", "J5 Roll", "J6 Grip"]
JOINT_COLORS = ["red", "green", "blue", "yellow", "cyan", "magenta"]
PLOT_JOINTS = [1, 2, 3]  # indices into JOINT_NAMES (0-based): J2, J3, J4


# =====================================================================
#  Joint state collector (runs via rclpy in a background thread)
# =====================================================================
class JointStateCollector:
    def __init__(self):
        self.lock = threading.Lock()
        self.times = deque(maxlen=HISTORY_SIZE)
        self.positions = [deque(maxlen=HISTORY_SIZE) for _ in range(6)]
        self.efforts = [deque(maxlen=HISTORY_SIZE) for _ in range(6)]
        self.start_time = time.time()

    def callback(self, msg):
        with self.lock:
            t = time.time() - self.start_time
            self.times.append(t)
            for i in range(min(6, len(msg.position))):
                self.positions[i].append(msg.position[i])
            for i in range(min(6, len(msg.effort))):
                self.efforts[i].append(msg.effort[i])

    def get_data(self):
        with self.lock:
            times = list(self.times)
            pos = [list(p) for p in self.positions]
            eff = [list(e) for e in self.efforts]
        return times, pos, eff


def start_ros_subscriber(collector):
    """Spin a minimal rclpy node in a daemon thread."""
    rclpy.init()
    node = rclpy.create_node('pid_hot_reload')
    node.create_subscription(JointState, '/mars/arm/state', collector.callback, 10)
    thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    thread.start()
    return node


# =====================================================================
#  ASCII plot renderer
# =====================================================================
def draw_plot(collector, status_line=""):
    times, positions, efforts = collector.get_data()
    if len(times) < 2:
        plt.clear_figure()
        plt.title("Waiting for joint state data...")
        plt.show()
        return

    # Only show the last HISTORY_SECONDS
    t_max = times[-1]
    t_min = t_max - HISTORY_SECONDS

    plt.clear_figure()
    plt.subplots(2, 1)

    # --- Top: joint positions ---
    plt.subplot(1, 1)
    title = "Joint Positions (rad)"
    if status_line:
        title += "  |  " + status_line
    plt.title(title)
    for i in PLOT_JOINTS:
        plt.plot(times, positions[i], label=JOINT_NAMES[i], color=JOINT_COLORS[i])
    plt.xlim(t_min, t_max)

    # --- Bottom: joint efforts (load %) ---
    plt.subplot(2, 1)
    plt.title("Joint Load (%)")
    for i in PLOT_JOINTS:
        plt.plot(times, efforts[i], label=JOINT_NAMES[i], color=JOINT_COLORS[i])
    plt.xlim(t_min, t_max)

    plt.show()


# =====================================================================
#  Config file watcher + param loader (unchanged logic)
# =====================================================================
def find_config_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    source_path = os.path.join(script_dir, '..', 'config', 'pid_tuning.yaml')
    if os.path.exists(source_path):
        return os.path.abspath(source_path)
    try:
        from ament_index_python.packages import get_package_share_directory
        pkg_dir = get_package_share_directory('maurice_arm')
        installed_path = os.path.join(pkg_dir, 'config', 'pid_tuning.yaml')
        if os.path.exists(installed_path):
            return installed_path
    except Exception:
        pass
    return None


def load_pid_config(path):
    with open(path, 'r') as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}
    params = {}
    for joint_key, gains in raw.items():
        if joint_key == 'gain_scheduling':
            # Pass gain scheduling as JSON string (same format as arm_config.yaml)
            gs_json = {"enabled": True}
            for profile in ('near', 'far'):
                if profile in gains and isinstance(gains[profile], dict):
                    gs_json[profile] = {}
                    for jkey, jgains in gains[profile].items():
                        if isinstance(jgains, dict):
                            gs_json[profile][jkey] = {k: int(v) for k, v in jgains.items() if k in ('kp', 'ki', 'kd')}
            params["gain_scheduling"] = json.dumps(gs_json)
            continue
        if not isinstance(gains, dict):
            continue
        for field in ('kp', 'ki', 'kd', 'profile_velocity', 'profile_acceleration'):
            if field in gains:
                params[f"{joint_key}_{field}"] = int(gains[field])
    return params


def apply_params(params, prev_params=None):
    node_name = '/maurice_arm'
    if prev_params is not None:
        changed = {k: v for k, v in params.items() if prev_params.get(k) != v}
    else:
        changed = dict(params)
    if not changed:
        return 0, ""

    ros2_params = {'/maurice_arm': {'ros__parameters': changed}}
    tmp_path = '/tmp/pid_hot_reload_params.yaml'
    with open(tmp_path, 'w') as f:
        yaml.dump(ros2_params, f)

    cmd = ['ros2', 'param', 'load', node_name, tmp_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(changed.items()))
            return len(changed), summary
        else:
            err = result.stderr.strip() or result.stdout.strip()
            return 0, f"FAILED: {err}"
    except Exception as e:
        return 0, f"ERROR: {e}"


# =====================================================================
#  Main
# =====================================================================
def main():
    no_plot = '--no-plot' in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    config_path = os.path.abspath(args[0]) if args else find_config_path()

    if config_path is None or not os.path.exists(config_path):
        print("Error: pid_tuning.yaml not found. Provide path as argument.")
        sys.exit(1)

    use_plot = HAS_PLOT and HAS_ROS and not no_plot

    print("╔══════════════════════════════════════════════════╗")
    print("║       Maurice Arm PID Hot-Reload Watcher        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  Watching: {config_path}")
    if not HAS_PLOT:
        print("  (install plotext for live plot: pip install plotext)")
    if not HAS_ROS:
        print("  (rclpy not found — plotting disabled)")
    print(f"  Plot: {'ON' if use_plot else 'OFF'}")
    print("  Press Ctrl+C to stop.\n")

    # Load and apply initial config
    try:
        prev_params = load_pid_config(config_path)
    except Exception as e:
        print(f"Error reading config: {e}")
        sys.exit(1)

    n, summary = apply_params(prev_params)
    status_line = f"Initial load: {n} params applied"
    if not use_plot:
        print(f"[{time.strftime('%H:%M:%S')}] {status_line}")
        if summary:
            print(f"  {summary}\n")

    # Start ROS2 subscriber for joint state data
    collector = None
    if use_plot:
        collector = JointStateCollector()
        start_ros_subscriber(collector)
        time.sleep(0.5)  # let subscription connect

    last_mtime = os.path.getmtime(config_path)
    plot_interval = 1.0 / PLOT_REFRESH_HZ

    # Main loop
    while True:
        try:
            time.sleep(plot_interval if use_plot else 0.5)

            # --- Check for file changes ---
            current_mtime = os.path.getmtime(config_path)
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                time.sleep(0.1)
                try:
                    new_params = load_pid_config(config_path)
                    if new_params != prev_params:
                        n, summary = apply_params(new_params, prev_params)
                        ts = time.strftime('%H:%M:%S')
                        if n > 0:
                            status_line = f"[{ts}] Updated {n} param(s): {summary}"
                        else:
                            status_line = f"[{ts}] {summary}"
                        prev_params = new_params
                        if not use_plot:
                            print(status_line)
                    else:
                        status_line = f"[{time.strftime('%H:%M:%S')}] Saved (no changes)"
                        if not use_plot:
                            print(status_line)
                except yaml.YAMLError as e:
                    status_line = f"YAML error: {e}"
                    if not use_plot:
                        print(status_line)
                except Exception as e:
                    status_line = f"Error: {e}"
                    if not use_plot:
                        print(status_line)

            # --- Redraw plot ---
            if use_plot and collector:
                try:
                    draw_plot(collector, status_line)
                except Exception as e:
                    print(f"\033[31mPlot error: {e}\033[0m")

        except KeyboardInterrupt:
            print("\nStopped. Goodbye!")
            if HAS_ROS:
                try:
                    rclpy.shutdown()
                except Exception:
                    pass
            break


if __name__ == '__main__':
    main()
