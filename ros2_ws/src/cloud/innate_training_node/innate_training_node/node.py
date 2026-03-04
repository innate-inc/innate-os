#!/usr/bin/env python3
"""
ROS 2 node for Innate training job management.

Publishes ``~/job_statuses`` (transient-local, default every 3 s) with
all tracked runs **and** active upload/download transfer snapshots.

Services: ``~/submit_skill``, ``~/create_run``, ``~/download_results``.

On startup fetches all existing jobs; auto-downloads + activates ``done`` runs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from innate_cloud_msgs.msg import TrainingJobList, TrainingParams, TransferProgress
from innate_cloud_msgs.srv import CreateRun, DownloadResults, SubmitSkill

from training_client.src.skill_manager import SkillManager, read_skill_id
from training_client.src.types import (
    ClientConfig,
    DEFAULT_AUTH_ISSUER_URL,
    DEFAULT_SERVER_URL,
    SkillInfo,
)

from .job_store import JobStore, build_skill_status
from .workers import Poller, do_upload, maybe_auto_download

# TODO: fetch from discovery URL once available.
KNOWN_PRESETS: set[str] = {"act-default"}


def _build_training_params(
    msg: TrainingParams,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert a ``TrainingParams`` msg to an API-ready dict.

    Returns ``(params_dict, error_string)``.  On success *error_string*
    is ``None``; on failure *params_dict* is ``None``.
    """
    if not msg.preset:
        return None, "preset is required"
    if msg.preset not in KNOWN_PRESETS:
        allowed = ", ".join(sorted(KNOWN_PRESETS))
        return None, f"unknown preset {msg.preset!r} — must be one of: {allowed}"

    params: dict[str, Any] = {}
    if msg.extra_json:
        try:
            params = json.loads(msg.extra_json)
        except json.JSONDecodeError as e:
            return None, f"Bad JSON: {e}"
        if not isinstance(params, dict):
            return None, "extra_json must be a JSON object"
    if msg.preset:
        params["preset"] = msg.preset
    if msg.env:
        params["env"] = list(msg.env)
    return (params or None), None


from dotenv import find_dotenv, load_dotenv


def _require_absolute(skill_dir: str) -> str | None:
    """Return an error string if *skill_dir* is not a non-empty absolute path."""
    if not skill_dir:
        return "skill_dir is required"
    if not os.path.isabs(skill_dir):
        return f"skill_dir must be an absolute path, got: {skill_dir}"
    return None


class _RosHandler(logging.Handler):
    """Forward stdlib log records to a ROS logger."""

    _LEVEL_MAP = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warn",
        logging.ERROR: "error",
        logging.CRITICAL: "fatal",
    }

    def __init__(self, ros_logger) -> None:
        super().__init__()
        self._ros = ros_logger

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        fn = self._LEVEL_MAP.get(record.levelno, "info")
        getattr(self._ros, fn)(msg)


class TrainingNode(Node):
    """Thin ROS 2 wiring: params → services → publisher timer."""

    def __init__(self) -> None:
        super().__init__("innate_training")

        # Bridge stdlib logs from training_client → ROS logger
        _lib_logger = logging.getLogger("training_client")
        _lib_logger.setLevel(logging.DEBUG)
        _lib_logger.addHandler(_RosHandler(self.get_logger()))

        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)
            self.get_logger().info(f"Loaded .env from {env_path}")
        else:
            self.get_logger().info("No .env file found")

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter(
            "server_url",
            os.getenv("TRAINING_SERVER_URL", DEFAULT_SERVER_URL),
        )
        self.declare_parameter(
            "service_key",
            os.getenv("INNATE_SERVICE_KEY", ""),
        )
        self.declare_parameter(
            "auth_issuer_url",
            os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
        )
        self.declare_parameter("poll_interval_sec", 3.0)
        self.declare_parameter("status_publish_interval_sec", 1.0)

        server_url = str(self.get_parameter("server_url").value)
        service_key = str(self.get_parameter("service_key").value)
        auth_issuer = str(self.get_parameter("auth_issuer_url").value)
        poll_sec = float(self.get_parameter("poll_interval_sec").value)
        pub_sec = float(self.get_parameter("status_publish_interval_sec").value)

        if not server_url or not service_key:
            self.get_logger().fatal("server_url and service_key are required")
            raise RuntimeError("server_url and service_key are required")

        # ── Shared objects ──────────────────────────────────────────
        config = ClientConfig(
            server_url=server_url,
            auth_token=service_key,
            auth_issuer_url=auth_issuer,
            poll_interval_seconds=poll_sec,
        )
        self._mgr: SkillManager = SkillManager(config)
        self._store: JobStore = JobStore()

        # ── Publisher ───────────────────────────────────────────────
        qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._pub: rclpy.publisher.Publisher = self.create_publisher(
            TrainingJobList, "~/job_statuses", qos
        )

        # ── Services ────────────────────────────────────────────────
        self.create_service(SubmitSkill, "~/submit_skill", self._on_submit)
        self.create_service(CreateRun, "~/create_run", self._on_create_run)
        self.create_service(DownloadResults, "~/download_results", self._on_download)

        # ── Timer + poller ──────────────────────────────────────────
        self.create_timer(pub_sec, self._publish)
        self._poller = Poller(self._mgr, self._store, poll_sec)
        self._poller.start()

        self.get_logger().info(
            f"Training node ready — server={server_url} poll={poll_sec}s pub={pub_sec}s"
        )

    def destroy_node(self) -> None:
        self._poller.stop()
        super().destroy_node()

    # ── Periodic publish ────────────────────────────────────────────

    def _publish(self) -> None:
        jobs, skills, transfers, completed, dir_map = self._store.snapshot()
        msg = TrainingJobList()
        msg.stamp = self.get_clock().now().to_msg()

        # Group runs by skill_id.
        runs_by_skill: dict[str, list] = defaultdict(list)
        for run in jobs:
            runs_by_skill[run.skill_id].append(run)

        # Collect all skill_ids (some skills may have no runs yet).
        all_skill_ids = set(runs_by_skill.keys()) | set(skills.keys())

        for sid in sorted(all_skill_ids):
            # Skill-level upload transfer (run_id == -1).
            upload_xfer = transfers.get((TransferProgress.UPLOAD, sid, -1))
            upload_done = (TransferProgress.UPLOAD, sid, -1) in completed

            # Per-run download transfers.
            dl_xfers: dict[int, TransferProgress] = {}
            dl_done: set[int] = set()
            for run in runs_by_skill.get(sid, []):
                key = (TransferProgress.DOWNLOAD, sid, run.run_id)
                xfer = transfers.get(key)
                if xfer is not None:
                    dl_xfers[run.run_id] = xfer
                if key in completed:
                    dl_done.add(run.run_id)

            msg.skills.append(
                build_skill_status(
                    sid,
                    skills.get(sid),
                    runs_by_skill.get(sid, []),
                    upload_xfer,
                    upload_done,
                    dl_xfers,
                    dl_done,
                    skill_dir=dir_map.get(sid, ""),
                )
            )

        self._pub.publish(msg)

    # ── Service: submit_skill ───────────────────────────────────────

    def _on_submit(
        self, req: SubmitSkill.Request, res: SubmitSkill.Response
    ) -> SubmitSkill.Response:
        """Submit (create-or-reuse) a skill **and** start uploading its data."""
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res

        try:
            gen = self._mgr.submit(req.skill_dir)
            skill: SkillInfo | None = None
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                skill = e.value

            if skill is None:
                res.success, res.message = False, "submit returned no skill"
                return res

            self._store.put_skill(skill)
            self._store.register_dir(skill.skill_id, req.skill_dir)

            # Mark upload as pending *before* spawning the thread and
            # returning the response, so the next published status already
            # reflects has_active_transfer=true / transfer_done=false.
            self._store.mark_upload_pending(skill.skill_id)

            # Start upload in a background thread.
            self.get_logger().info(
                f"Skill {skill.skill_id[:8]} ({skill.name}) submitted "
                f"— upload started from {req.skill_dir}"
            )
            threading.Thread(
                target=do_upload,
                args=(self._mgr, self._store, skill.skill_id, req.skill_dir),
                daemon=True,
                name=f"ul-{skill.skill_id[:8]}",
            ).start()

            res.success, res.skill_id = True, skill.skill_id
            res.message = f"Skill {skill.skill_id} submitted — upload started"
        except Exception as e:
            self.get_logger().error(f"submit failed: {e}")
            res.success, res.message = False, str(e)
        return res

    # ── Service: create_run ─────────────────────────────────────────

    def _on_create_run(
        self, req: CreateRun.Request, res: CreateRun.Response
    ) -> CreateRun.Response:
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res

        skill_id = read_skill_id(req.skill_dir)
        if not skill_id:
            res.success, res.message = (
                False,
                f"No training_skill_id in {req.skill_dir}/metadata.json — submit first",
            )
            return res

        training_params, params_err = _build_training_params(req.run_params)
        if params_err:
            res.success, res.message = False, params_err
            return res

        try:
            data = self._mgr.client.create_run(
                skill_id, training_params=training_params
            )
            rid = int(data["run_id"])
            self._store.put_job(self._mgr.run_status(skill_id, rid))
            self._store.register_dir(skill_id, req.skill_dir)
            res.success, res.run_id = True, rid
            res.message = f"Run {skill_id}/{rid} created"
            self.get_logger().info(
                f"Run {skill_id[:8]}/{rid} created"
                f" | preset={training_params.get('preset', '?') if training_params else '?'}"
            )
        except Exception as e:
            self.get_logger().error(f"create_run failed: {e}")
            res.success, res.message = False, str(e)
        return res

    # ── Service: download_results ───────────────────────────────────

    def _on_download(
        self, req: DownloadResults.Request, res: DownloadResults.Response
    ) -> DownloadResults.Response:
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res
        if req.run_id < 0:
            res.success, res.message = False, "non-negative run_id required"
            return res

        skill_id = read_skill_id(req.skill_dir)
        if not skill_id:
            res.success, res.message = (
                False,
                f"No training_skill_id in {req.skill_dir}/metadata.json — submit first",
            )
            return res

        self._store.register_dir(skill_id, req.skill_dir)
        self.get_logger().info(
            f"Download started for {skill_id}/{req.run_id} → {req.skill_dir}"
        )
        maybe_auto_download(self._mgr, self._store, skill_id, req.run_id, req.skill_dir)
        res.success, res.message = True, f"Download started → {req.skill_dir}"
        return res


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = TrainingNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
