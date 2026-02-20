#!/usr/bin/env python3
"""
ROS 2 node for Innate training job management.

Publishes ``~/job_statuses`` (transient-local, default every 3 s) with
all tracked runs **and** active upload/download transfer snapshots.

Services: ``~/submit_skill``, ``~/upload_skill``, ``~/create_run``,
``~/download_results``.

On startup fetches all existing jobs; auto-downloads ``done`` runs.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from innate_cloud_msgs.msg import TrainingJobList, TrainingParams, TransferProgress
from innate_cloud_msgs.srv import CreateRun, DownloadResults, SubmitSkill, UploadSkill

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


def _load_dotenv() -> Path | None:
    """Load the first ``.env`` found walking up from cwd, then ``~/innate-os/.env``.

    Already-set env vars are NOT overwritten.
    Returns the path that was loaded, or ``None`` if no ``.env`` was found.
    """
    candidates: list[Path] = []
    d = Path.cwd()
    while True:
        candidates.append(d / ".env")
        if d.parent == d:
            break
        d = d.parent
    candidates.append(Path.home() / "innate-os" / ".env")

    for path in candidates:
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                os.environ.setdefault(key, value)
            return path
    return None


def _require_absolute(skill_dir: str) -> str | None:
    """Return an error string if *skill_dir* is not a non-empty absolute path."""
    if not skill_dir:
        return "skill_dir is required"
    if not os.path.isabs(skill_dir):
        return f"skill_dir must be an absolute path, got: {skill_dir}"
    return None


class TrainingNode(Node):
    """Thin ROS 2 wiring: params → services → publisher timer."""

    def __init__(self) -> None:
        super().__init__("innate_training")
        env_path = _load_dotenv()
        if env_path is not None:
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
        self.create_service(UploadSkill, "~/upload_skill", self._on_upload)
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
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res

        try:
            gen = self._mgr.submit(req.skill_dir, name=req.name or None)
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
            res.success, res.skill_id = True, skill.skill_id
            res.message = f"Skill {skill.skill_id} created"
        except Exception as e:
            self.get_logger().error(f"submit failed: {e}")
            res.success, res.message = False, str(e)
        return res

    # ── Service: upload_skill ───────────────────────────────────────

    def _on_upload(
        self, req: UploadSkill.Request, res: UploadSkill.Response
    ) -> UploadSkill.Response:
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res

        skill_id = read_skill_id(req.skill_dir)
        if not skill_id:
            res.success, res.message = (
                False,
                f"No server-skill.json in {req.skill_dir} — submit first",
            )
            return res

        self._store.register_dir(skill_id, req.skill_dir)
        self.get_logger().info(f"Upload started for {skill_id} from {req.skill_dir}")
        threading.Thread(
            target=do_upload,
            args=(self._mgr, self._store, skill_id, req.skill_dir),
            daemon=True,
            name=f"ul-{skill_id[:8]}",
        ).start()

        res.success = True
        res.message = f"Upload started for {skill_id} from {req.skill_dir}"
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
                f"No server-skill.json in {req.skill_dir} — submit first",
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
                f"No server-skill.json in {req.skill_dir} — submit first",
            )
            return res

        self._store.register_dir(skill_id, req.skill_dir)
        self._store.mark_download_started(skill_id, req.run_id)
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
