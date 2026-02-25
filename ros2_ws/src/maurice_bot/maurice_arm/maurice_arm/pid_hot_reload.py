#!/usr/bin/env python3
"""
PID Hot-Reload Watcher with Real-Time Joint State Plot.

Watches arm_config.yaml for changes and applies parameters to the running
arm node via `ros2 param load`. Optionally plots joint positions in real-time.

Usage:
    python3 pid_hot_reload.py [path/to/arm_config.yaml] [--no-plot]
"""

import os
import subprocess
import sys
import threading
import time
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
    from sensor_msgs.msg import JointState

    HAS_ROS = True
except ImportError:
    HAS_ROS = False

# --- Configuration ---
HISTORY_SECONDS = 5
HISTORY_SIZE = 500  # samples to keep (~5s at 100 Hz)
PLOT_REFRESH_HZ = 4  # plot redraws per second
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
    node = rclpy.create_node("pid_hot_reload")
    node.create_subscription(JointState, "/mars/arm/state", collector.callback, 10)
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
#  Config file watcher + param loader
# =====================================================================
NODE_NAME = "/maurice_arm"


def find_config_path():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    source_path = os.path.join(script_dir, "..", "config", "arm_config.yaml")
    if os.path.exists(source_path):
        return os.path.abspath(source_path)
    try:
        from ament_index_python.packages import get_package_share_directory

        pkg_dir = get_package_share_directory("maurice_arm")
        installed_path = os.path.join(pkg_dir, "config", "arm_config.yaml")
        if os.path.exists(installed_path):
            return installed_path
    except Exception:
        pass
    return None


def apply_config(config_path):
    """Apply config by loading it directly as ROS 2 parameters.
    
    The YAML is now standard ros__parameters format (no JSON blobs),
    so `ros2 param load` handles everything natively.
    """
    cmd = ["ros2", "param", "load", NODE_NAME, config_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return True, "params loaded"
        else:
            err = result.stderr.strip() or result.stdout.strip()
            return False, f"param load failed: {err}"
    except Exception as e:
        return False, f"param load error: {e}"


# =====================================================================
#  Main
# =====================================================================
def main():
    no_plot = "--no-plot" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    config_path = os.path.abspath(args[0]) if args else find_config_path()

    if config_path is None or not os.path.exists(config_path):
        print("Error: arm_config.yaml not found. Provide path as argument.")
        sys.exit(1)

    use_plot = HAS_PLOT and HAS_ROS and not no_plot

    print("╔══════════════════════════════════════════════════╗")
    print("║       Maurice Arm PID Hot-Reload Watcher        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  Watching: {config_path}")
    print(f"  Method:   ros2 param load {NODE_NAME} <file>")
    if not HAS_PLOT:
        print("  (install plotext for live plot: pip install plotext)")
    if not HAS_ROS:
        print("  (rclpy not found — plotting disabled)")
    print(f"  Plot: {'ON' if use_plot else 'OFF'}")
    print("  Press Ctrl+C to stop.\n")

    # Initial load
    ok, summary = apply_config(config_path)
    status_line = f"Initial load: {'OK' if ok else 'FAILED'} — {summary}"
    if not use_plot:
        print(f"[{time.strftime('%H:%M:%S')}] {status_line}")

    # Start ROS2 subscriber for joint state plotting
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
                time.sleep(0.1)  # let editor finish writing
                ok, summary = apply_config(config_path)
                ts = time.strftime("%H:%M:%S")
                status_line = f"[{ts}] {'OK' if ok else 'FAILED'}: {summary}"
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


if __name__ == "__main__":
    main()
