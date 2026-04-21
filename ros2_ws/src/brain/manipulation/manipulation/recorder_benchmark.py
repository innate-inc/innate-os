#!/usr/bin/env python3
"""Recorder benchmark harness.

Drives the recorder through N back-to-back episodes while sampling the
recorder process's CPU, RAM, and disk I/O. Writes results to CSV so we
can compare resource usage before/after the streaming HDF5 refactor.

Prerequisites:
  - recorder_node_cpp running (from manipulation package)
  - brain_client skills_action_server running (provides /brain/create_physical_skill)
"""

import argparse
import csv
import os
import sys
import threading
import time
from datetime import datetime

import psutil
import rclpy
from rclpy.node import Node
from std_srvs.srv import Trigger

from brain_messages.srv import ActivateManipulationTask, CreatePhysicalSkill


def find_recorder_pid():
    """Find the PID of the running recorder_node_cpp process."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "recorder_node_cpp" in name or "recorder_node_cpp" in cmdline:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


class ResourceSampler:
    """Background thread that samples a process's CPU/RAM/IO at a given rate."""

    def __init__(self, pid, sample_hz=10.0):
        self.proc = psutil.Process(pid)
        self.period = 1.0 / sample_hz
        self.samples = []
        self.phase = "idle"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._t0 = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Prime CPU percent (first call returns 0.0)
        self.proc.cpu_percent(interval=None)

    def set_phase(self, phase):
        with self._lock:
            self.phase = phase

    def start(self):
        self._t0 = time.monotonic()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self):
        try:
            num_cores = psutil.cpu_count(logical=True) or 1
        except Exception:
            num_cores = 1

        while not self._stop.is_set():
            try:
                with self.proc.oneshot():
                    cpu = self.proc.cpu_percent(interval=None)
                    mem = self.proc.memory_info()
                    nthreads = self.proc.num_threads()
                    try:
                        io = self.proc.io_counters()
                        read_bytes = io.read_bytes
                        write_bytes = io.write_bytes
                    except (psutil.AccessDenied, AttributeError):
                        read_bytes = -1
                        write_bytes = -1
                t = time.monotonic() - self._t0
                with self._lock:
                    phase = self.phase
                self.samples.append({
                    "t_sec": round(t, 3),
                    "phase": phase,
                    "cpu_pct": round(cpu, 2),
                    "cpu_pct_normalized": round(cpu / num_cores, 2),
                    "rss_mb": round(mem.rss / (1024 * 1024), 2),
                    "vms_mb": round(mem.vms / (1024 * 1024), 2),
                    "num_threads": nthreads,
                    "read_bytes": read_bytes,
                    "write_bytes": write_bytes,
                })
            except psutil.NoSuchProcess:
                break
            except Exception as e:
                # Best-effort: keep sampling even if one tick fails
                print(f"[sampler] tick error: {e}", file=sys.stderr)

            self._stop.wait(self.period)


class RecorderBenchClient(Node):
    """ROS2 client that drives the recorder services."""

    def __init__(self):
        super().__init__("recorder_benchmark_client")

        self.create_skill_cli = self.create_client(
            CreatePhysicalSkill, "/brain/create_physical_skill"
        )
        self.activate_cli = self.create_client(
            ActivateManipulationTask,
            "/brain/recorder/activate_physical_primitive",
        )
        self.new_episode_cli = self.create_client(
            Trigger, "/brain/recorder/new_episode"
        )
        self.stop_episode_cli = self.create_client(
            Trigger, "/brain/recorder/stop_episode"
        )
        self.save_episode_cli = self.create_client(
            Trigger, "/brain/recorder/save_episode"
        )
        self.cancel_episode_cli = self.create_client(
            Trigger, "/brain/recorder/cancel_episode"
        )
        self.end_task_cli = self.create_client(
            Trigger, "/brain/recorder/end_task"
        )

    def _wait_for_service(self, client, name, timeout=10.0):
        self.get_logger().info(f"Waiting for service: {name}")
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError(f"Service {name} not available after {timeout}s")

    def wait_for_all_services(self):
        self._wait_for_service(self.create_skill_cli, "/brain/create_physical_skill")
        self._wait_for_service(
            self.activate_cli, "/brain/recorder/activate_physical_primitive"
        )
        self._wait_for_service(self.new_episode_cli, "/brain/recorder/new_episode")
        self._wait_for_service(self.stop_episode_cli, "/brain/recorder/stop_episode")
        self._wait_for_service(self.save_episode_cli, "/brain/recorder/save_episode")
        self._wait_for_service(self.end_task_cli, "/brain/recorder/end_task")

    def _call(self, client, request, label, timeout=120.0):
        """Synchronous call with timing. Returns (response, elapsed_sec)."""
        t0 = time.monotonic()
        future = client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout)
        if not future.done():
            raise RuntimeError(f"{label} timed out after {timeout}s")
        elapsed = time.monotonic() - t0
        resp = future.result()
        if resp is None:
            raise RuntimeError(f"{label} returned no response")
        success = getattr(resp, "success", True)
        if not success:
            msg = getattr(resp, "message", "<no message>")
            raise RuntimeError(f"{label} failed: {msg}")
        self.get_logger().info(f"{label} OK ({elapsed * 1000:.1f} ms)")
        return resp, elapsed

    def create_skill(self, name):
        req = CreatePhysicalSkill.Request()
        req.name = name
        resp, _ = self._call(self.create_skill_cli, req, f"create_physical_skill({name})")
        return resp.skill_directory

    def activate(self, task_directory):
        req = ActivateManipulationTask.Request()
        req.task_directory = task_directory
        self._call(self.activate_cli, req, "activate_physical_primitive")

    def new_episode(self):
        self._call(self.new_episode_cli, Trigger.Request(), "new_episode")

    def stop_episode(self):
        self._call(self.stop_episode_cli, Trigger.Request(), "stop_episode")

    def save_episode(self):
        _, elapsed = self._call(
            self.save_episode_cli, Trigger.Request(), "save_episode"
        )
        return elapsed

    def end_task(self):
        self._call(self.end_task_cli, Trigger.Request(), "end_task")


def write_csv(samples, path):
    if not samples:
        return
    fieldnames = list(samples[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(samples)


def write_summary(samples, save_durations, args, output_dir, summary_path):
    if not samples:
        return

    rss_values = [s["rss_mb"] for s in samples]
    cpu_values = [s["cpu_pct"] for s in samples]
    cpu_norm_values = [s["cpu_pct_normalized"] for s in samples]

    write_bytes = [s["write_bytes"] for s in samples if s["write_bytes"] >= 0]
    total_written_mb = (
        (write_bytes[-1] - write_bytes[0]) / (1024 * 1024) if len(write_bytes) >= 2 else 0
    )

    # Per-phase stats
    by_phase = {}
    for s in samples:
        by_phase.setdefault(s["phase"], []).append(s)

    with open(summary_path, "w") as f:
        f.write("Recorder Benchmark Summary\n")
        f.write("=" * 50 + "\n")
        f.write(f"Timestamp:           {datetime.now().isoformat()}\n")
        f.write(f"Output dir:          {output_dir}\n")
        f.write(f"Episodes:            {args.num_episodes}\n")
        f.write(f"Episode duration:    {args.episode_duration}s\n")
        f.write(f"Sample rate:         {args.sample_hz} Hz\n")
        f.write(f"Total samples:       {len(samples)}\n")
        f.write(f"Test wall time:      {samples[-1]['t_sec']:.1f}s\n\n")

        f.write("Memory (RSS)\n")
        f.write(f"  peak:  {max(rss_values):.1f} MB\n")
        f.write(f"  mean:  {sum(rss_values) / len(rss_values):.1f} MB\n")
        f.write(f"  min:   {min(rss_values):.1f} MB\n\n")

        f.write("CPU (raw, can exceed 100% on multi-core)\n")
        f.write(f"  peak:  {max(cpu_values):.1f}%\n")
        f.write(f"  mean:  {sum(cpu_values) / len(cpu_values):.1f}%\n\n")

        f.write("CPU (normalized: % of 1 core / N cores)\n")
        f.write(f"  peak:  {max(cpu_norm_values):.1f}%\n")
        f.write(f"  mean:  {sum(cpu_norm_values) / len(cpu_norm_values):.1f}%\n\n")

        f.write(f"Total bytes written: {total_written_mb:.1f} MB\n\n")

        f.write("save_episode latencies\n")
        for i, dur in enumerate(save_durations, 1):
            f.write(f"  episode {i}: {dur * 1000:.1f} ms\n")
        if save_durations:
            f.write(f"  mean:      {sum(save_durations) * 1000 / len(save_durations):.1f} ms\n")
            f.write(f"  max:       {max(save_durations) * 1000:.1f} ms\n\n")

        f.write("Per-phase peak RSS\n")
        for phase, phase_samples in by_phase.items():
            phase_rss = [s["rss_mb"] for s in phase_samples]
            f.write(
                f"  {phase:32s} peak={max(phase_rss):.1f} MB  "
                f"mean={sum(phase_rss) / len(phase_rss):.1f} MB  "
                f"n={len(phase_samples)}\n"
            )


def main():
    parser = argparse.ArgumentParser(description="Recorder resource benchmark")
    parser.add_argument("--num-episodes", type=int, default=5)
    parser.add_argument("--episode-duration", type=float, default=30.0)
    parser.add_argument("--sample-hz", type=float, default=10.0)
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.expanduser("~/recorder_bench"),
    )
    parser.add_argument(
        "--skill-name-prefix", type=str, default="bench"
    )
    args = parser.parse_args()

    pid = find_recorder_pid()
    if pid is None:
        print("ERROR: recorder_node_cpp process not found. Is it running?",
              file=sys.stderr)
        sys.exit(1)
    print(f"Found recorder PID: {pid}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(args.output_dir, timestamp)
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, "samples.csv")
    summary_path = os.path.join(output_dir, "summary.txt")
    print(f"Output dir: {output_dir}")

    sampler = ResourceSampler(pid, sample_hz=args.sample_hz)

    rclpy.init()
    client = RecorderBenchClient()

    save_durations = []
    skill_name = f"{args.skill_name_prefix}-{timestamp}"

    try:
        client.wait_for_all_services()
        sampler.start()

        sampler.set_phase("create_skill")
        skill_dir = client.create_skill(skill_name)
        print(f"Skill directory: {skill_dir}")

        sampler.set_phase("activate")
        client.activate(skill_dir)

        for i in range(1, args.num_episodes + 1):
            phase_record = f"episode_{i}_recording"
            phase_save = f"episode_{i}_saving"

            sampler.set_phase(phase_record)
            print(f"\n=== Episode {i}/{args.num_episodes}: recording {args.episode_duration}s ===")
            client.new_episode()
            time.sleep(args.episode_duration)
            client.stop_episode()

            sampler.set_phase(phase_save)
            print(f"=== Episode {i}: saving ===")
            save_dur = client.save_episode()
            save_durations.append(save_dur)

            # Brief idle gap so we can see the drop-off in samples
            sampler.set_phase(f"episode_{i}_idle")
            time.sleep(1.0)

        sampler.set_phase("end_task")
        client.end_task()

        sampler.set_phase("idle_post")
        time.sleep(1.0)

    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
    finally:
        sampler.stop()
        write_csv(sampler.samples, csv_path)
        write_summary(sampler.samples, save_durations, args, output_dir, summary_path)
        print(f"\nWrote {len(sampler.samples)} samples to {csv_path}")
        print(f"Wrote summary to {summary_path}")
        print("\n" + "-" * 50)
        with open(summary_path) as f:
            print(f.read())

        try:
            client.destroy_node()
        except Exception:
            pass
        rclpy.shutdown()


if __name__ == "__main__":
    main()
