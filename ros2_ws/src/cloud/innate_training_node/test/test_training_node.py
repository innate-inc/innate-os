#!/usr/bin/env python3
"""
CLI test client for the innate_training ROS 2 node.

Exercises all three services and subscribes to job_statuses.
Designed to run side-by-side with the node so you can watch
status updates flow in real time.

Usage (in a sourced workspace):

    # Terminal 1 — start the node
    ros2 run innate_training_node training_node \
        --ros-args -p service_key:=<key>

    # Terminal 2 — run commands
    python3 test/test_training_node.py submit /path/to/skill --name my_skill
    python3 test/test_training_node.py run /path/to/skill --preset default
    python3 test/test_training_node.py download /path/to/skill <run_id>
    python3 test/test_training_node.py watch

Requires: ``pip install click``
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone

import click
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from innate_cloud_msgs.msg import (
    TrainingJobList,
    TrainingParams,
    TrainingRunStatus,
    TrainingSkillStatus,
    TransferProgress,
)
from innate_cloud_msgs.srv import CreateRun, DownloadResults, SubmitSkill

# ── Pretty-print helpers ────────────────────────────────────────────

_RUN_STATUS_NAMES: dict[int, str] = {
    TrainingRunStatus.STATUS_UNKNOWN: "UNKNOWN",
    TrainingRunStatus.STATUS_WAITING_FOR_APPROVAL: "WAITING_FOR_APPROVAL",
    TrainingRunStatus.STATUS_APPROVED: "APPROVED",
    TrainingRunStatus.STATUS_REJECTED: "REJECTED",
    TrainingRunStatus.STATUS_BOOTING: "BOOTING",
    TrainingRunStatus.STATUS_RUNNING: "RUNNING",
    TrainingRunStatus.STATUS_DONE: "DONE",
    TrainingRunStatus.STATUS_DOWNLOADED: "DOWNLOADED",
}

_DIRECTION_NAMES: dict[int, str] = {
    TransferProgress.UPLOAD: "UP",
    TransferProgress.DOWNLOAD: "DL",
}

_STAGE_NAMES: dict[int, str] = {
    TransferProgress.STAGE_COMPRESSING: "COMPRESSING",
    TransferProgress.STAGE_UPLOADING: "UPLOADING",
    TransferProgress.STAGE_DOWNLOADING: "DOWNLOADING",
    TransferProgress.STAGE_VERIFYING: "VERIFYING",
    TransferProgress.STAGE_DONE: "DONE",
    TransferProgress.STAGE_ERROR: "ERROR",
}

_SVC_TIMEOUT_SEC = 30.0
_NODE_NS = "/innate_training"


def _ros_time_str(t: object) -> str:
    """Format a builtin_interfaces/Time as ISO-8601, or '—' if zero."""
    sec: int = getattr(t, "sec", 0)
    nanosec: int = getattr(t, "nanosec", 0)
    if sec == 0 and nanosec == 0:
        return "—"
    dt = datetime.fromtimestamp(sec + nanosec / 1e9, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_transfer(xfer: TransferProgress) -> str:
    direction = _DIRECTION_NAMES.get(xfer.direction, "?")
    stage = _STAGE_NAMES.get(xfer.stage, "?")
    pct = ""
    if xfer.bytes_total > 0:
        pct = f" {xfer.bytes_done / xfer.bytes_total * 100:.1f}%"
    file_info = ""
    if xfer.file_total > 0:
        file_info = f" file {xfer.file_index}/{xfer.file_total}"
    return f"[{direction} {stage}{pct}{file_info}] {xfer.message}"


def _fmt_run(run: TrainingRunStatus, indent: str = "    ") -> str:
    status = _RUN_STATUS_NAMES.get(run.status, f"?({run.status})")
    lines = [f"{indent}Run {run.run_id}: {status}"]
    if run.daemon_state:
        lines.append(f"{indent}  daemon: {run.daemon_state}")
    if run.error_message:
        lines.append(f"{indent}  error:  {run.error_message}")
    if run.instance_type:
        lines.append(f"{indent}  type:   {run.instance_type}")
    lines.append(
        f"{indent}  started: {_ros_time_str(run.started_at)}  "
        f"finished: {_ros_time_str(run.finished_at)}"
    )
    if run.transfer_done:
        lines.append(f"{indent}  ✓ download complete")
    if run.has_active_transfer:
        lines.append(f"{indent}  transfer: {_fmt_transfer(run.active_transfer)}")
    return "\n".join(lines)


def _fmt_skill(skill: TrainingSkillStatus) -> str:
    name = skill.skill_name or "(unnamed)"
    lines = [f"  Skill {skill.skill_id}  ({name})"]
    if skill.skill_dir:
        lines.append(f"    dir: {skill.skill_dir}")
    if skill.transfer_done:
        lines.append("    ✓ upload complete")
    if skill.has_active_transfer:
        lines.append(f"    transfer: {_fmt_transfer(skill.active_transfer)}")
    if not skill.runs:
        lines.append("    (no runs)")
    for run in skill.runs:
        lines.append(_fmt_run(run))
    return "\n".join(lines)


def fmt_job_list(msg: TrainingJobList) -> str:
    """Pretty-format a TrainingJobList message."""
    header = f"─── job_statuses  stamp={_ros_time_str(msg.stamp)} ───"
    if not msg.skills:
        return f"{header}\n  (no skills)"
    parts = [header]
    for skill in msg.skills:
        parts.append(_fmt_skill(skill))
    return "\n".join(parts)


# ── ROS 2 helpers ───────────────────────────────────────────────────


def _make_node(name: str = "test_training_client") -> Node:
    """Init rclpy (if needed) and return a fresh Node."""
    rclpy.init()
    return Node(name)


def _call_service(
    node: Node,
    srv_type: type,
    srv_name: str,
    request: object,
) -> object | None:
    """Create a client, wait for the service, call it, return the response."""
    client = node.create_client(srv_type, f"{_NODE_NS}/{srv_name}")
    if not client.wait_for_service(timeout_sec=5.0):
        click.secho(
            f"✗ Service {srv_name} not available (is the node running?)",
            fg="red",
        )
        return None
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=_SVC_TIMEOUT_SEC)
    return future.result()


# ── CLI ─────────────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """Test client for the innate_training ROS 2 node."""


@cli.command()
@click.argument("skill_dir")
@click.option("--name", default="", help="Skill name (defaults to directory name).")
def submit(skill_dir: str, name: str) -> None:
    """Create a skill on the server from a local directory."""
    node = _make_node()
    try:
        req = SubmitSkill.Request()
        req.skill_dir = skill_dir
        req.name = name
        res = _call_service(node, SubmitSkill, "submit_skill", req)
        if res is None:
            click.secho("✗ No response (timeout?)", fg="red")
        elif res.success:
            click.secho(f"✓ Skill created: {res.skill_id}", fg="green")
            click.echo(f"  {res.message}")
        else:
            click.secho(f"✗ {res.message}", fg="red")
    finally:
        node.destroy_node()
        rclpy.shutdown()


@cli.command("run")
@click.argument("skill_dir")
@click.option("--preset", default="", help="Training preset name.")
@click.option(
    "--env", multiple=True, help="Environment overrides as KEY=VALUE (repeatable)."
)
@click.option(
    "--json", "extra_json", default="", help="Extra params as a JSON object string."
)
def create_run(
    skill_dir: str, preset: str, env: tuple[str, ...], extra_json: str
) -> None:
    """Create a training run for a skill identified by its local directory."""
    if extra_json:
        try:
            obj = json.loads(extra_json)
        except json.JSONDecodeError as e:
            raise click.BadParameter(f"Bad JSON: {e}", param_hint="--json") from e
        if not isinstance(obj, dict):
            raise click.BadParameter("Must be a JSON object", param_hint="--json")

    node = _make_node()
    try:
        req = CreateRun.Request()
        req.skill_dir = skill_dir
        req.run_params = TrainingParams()
        req.run_params.preset = preset
        req.run_params.env = list(env)
        req.run_params.extra_json = extra_json
        res = _call_service(node, CreateRun, "create_run", req)
        if res is None:
            click.secho("✗ No response (timeout?)", fg="red")
        elif res.success:
            click.secho(f"✓ Run created: {res.run_id}", fg="green")
            click.echo(f"  {res.message}")
        else:
            click.secho(f"✗ {res.message}", fg="red")
    finally:
        node.destroy_node()
        rclpy.shutdown()


@cli.command()
@click.argument("skill_dir")
@click.argument("run_id", type=int)
def download(skill_dir: str, run_id: int) -> None:
    """Download results for a completed run."""
    node = _make_node()
    try:
        req = DownloadResults.Request()
        req.skill_dir = skill_dir
        req.run_id = run_id
        res = _call_service(node, DownloadResults, "download_results", req)
        if res is None:
            click.secho("✗ No response (timeout?)", fg="red")
        elif res.success:
            click.secho(f"✓ {res.message}", fg="green")
        else:
            click.secho(f"✗ {res.message}", fg="red")
    finally:
        node.destroy_node()
        rclpy.shutdown()


@cli.command()
def watch() -> None:
    """Subscribe to job_statuses and print updates continuously (Ctrl-C to stop)."""
    node = _make_node("test_training_watcher")
    try:
        qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )

        def _on_msg(msg: TrainingJobList) -> None:
            click.echo(f"\n{fmt_job_list(msg)}\n")

        node.create_subscription(
            TrainingJobList,
            f"{_NODE_NS}/job_statuses",
            _on_msg,
            qos,
        )
        click.echo("Watching job_statuses… (Ctrl-C to stop)")
        rclpy.spin(node)
    except KeyboardInterrupt:
        click.echo("\nStopped.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    cli()
