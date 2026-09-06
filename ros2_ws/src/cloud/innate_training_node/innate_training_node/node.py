#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
ROS 2 node for Innate training job management.

Publishes ``~/job_statuses`` (transient-local, default every 3 s) with
all tracked runs **and** active upload/download transfer snapshots.

Services: ``~/submit_skill``, ``~/create_run``, ``~/download_results``.

On startup fetches all existing jobs; auto-downloads + activates ``done`` runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import defaultdict
from typing import Any

import rclpy
from innate_cloud_msgs.msg import TrainingJobList, TrainingParams, TransferProgress
from innate_cloud_msgs.srv import (
    CancelRun,
    CreateRun,
    DownloadResults,
    GetRunLogs,
    GetTrainingStatus,
    StartTraining,
    SubmitSkill,
)
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from training_client.src.skill_manager import (
    SkillManager,
    read_skill_id,
)
from training_client.src.skill_manager import (
    read_uploaded_episode_count as _read_ep_count_from_disk,
)
from training_client.src.types import (
    DEFAULT_AUTH_ISSUER_URL,
    DEFAULT_SERVER_URL,
    ClientConfig,
    SkillInfo,
)

from .job_store import JobStore, build_skill_status
from .workers import Poller, do_upload, maybe_auto_download, start_prebuild_sweep

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
        env_dict: dict[str, str] = {}
        for entry in msg.env:
            key, _, value = entry.partition("=")
            if key:
                env_dict[key] = value
        if env_dict:
            params["env"] = env_dict
    return (params or None), None


from dotenv import find_dotenv, load_dotenv  # noqa: E402


def _require_absolute(skill_dir: str) -> str | None:
    """Return an error string if *skill_dir* is not a non-empty absolute path."""
    if not skill_dir:
        return "skill_dir is required"
    if not os.path.isabs(skill_dir):
        return f"skill_dir must be an absolute path, got: {skill_dir}"
    return None


class _RosHandler(logging.Handler):
    """Forward stdlib log records to a ROS logger.

    rclpy (Humble) caches severity per call-site ``(file, line, function)``
    and rejects changes with ``"Logger severity cannot be changed between
    calls"``.  Dispatch each severity from its own source line so the
    caller_id differs per level.
    """

    def __init__(self, ros_logger) -> None:
        super().__init__()
        self._ros = ros_logger

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        level = record.levelno
        if level >= logging.CRITICAL:
            self._ros.fatal(msg)
        elif level >= logging.ERROR:
            self._ros.error(msg)
        elif level >= logging.WARNING:
            self._ros.warn(msg)
        elif level >= logging.INFO:
            self._ros.info(msg)
        else:
            self._ros.debug(msg)


class TrainingNode(Node):
    """Thin ROS 2 wiring: params → services → publisher timer."""

    def __init__(self) -> None:
        super().__init__("innate_training")

        # Bridge stdlib logs from training_client + auth_client → ROS logger.
        # auth_client emits its cold-boot retry progress here via wait_for_token.
        # Python's stdlib loggers are module-global singletons, so if
        # TrainingNode is constructed more than once in the same process
        # (tests, composable-node containers) we must evict any handlers
        # installed by a prior instance to avoid duplicate emission.
        _ros_handler = _RosHandler(self.get_logger())
        for _name in ("training_client", "auth_client"):
            _lib_logger = logging.getLogger(_name)
            for _stale in [h for h in _lib_logger.handlers if isinstance(h, _RosHandler)]:
                _lib_logger.removeHandler(_stale)
            _lib_logger.setLevel(logging.DEBUG)
            _lib_logger.addHandler(_ros_handler)

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
            "auth_issuer_url",
            os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
        )
        self.declare_parameter("poll_interval_sec", 3.0)
        self.declare_parameter("status_publish_interval_sec", 1.0)

        server_url = str(self.get_parameter("server_url").value)
        # Never declare credentials as ROS params: rosbridge can read them.
        service_key = os.getenv("INNATE_SERVICE_KEY", "")
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
        # OrchestratorClient uses AuthProvider.wait_for_token() internally
        # so cold-boot auth failures (DNS not ready, no RTC → TLS notBefore)
        # are retried with exponential backoff instead of crashing init.
        self._mgr: SkillManager = SkillManager(config)
        self._store: JobStore = JobStore()

        # ── Publisher ───────────────────────────────────────────────
        qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._pub: rclpy.publisher.Publisher = self.create_publisher(TrainingJobList, "~/job_statuses", qos)

        # ── Services ────────────────────────────────────────────────
        self.create_service(SubmitSkill, "~/submit_skill", self._on_submit)
        self.create_service(CreateRun, "~/create_run", self._on_create_run)
        self.create_service(StartTraining, "~/start_training", self._on_start_training)
        self.create_service(CancelRun, "~/cancel_run", self._on_cancel_run)
        self.create_service(GetRunLogs, "~/get_run_logs", self._on_get_run_logs)
        self.create_service(DownloadResults, "~/download_results", self._on_download)
        self.create_service(GetTrainingStatus, "~/get_training_status", self._on_get_training_status)

        # Map cloud skill_ids back to their local directories from disk, so the
        # published status carries skill_dir even after a restart (the registry
        # is in-memory; without this, locally-recorded skills look "foreign").
        self._register_local_skill_dirs()

        # Resume any start_training that a crash/reboot interrupted between
        # upload and create_run (a robot can power-cycle anytime).
        self._resume_pending_training()

        # ── Timer + poller ──────────────────────────────────────────
        self.create_timer(pub_sec, self._publish)
        self._poller = Poller(self._mgr, self._store, poll_sec)
        self._poller.start()

        # Warm TensorRT engines for any local checkpoints missing a current-version
        # engine — models downloaded before pre-build existed, shipped skills, or
        # engines invalidated by a TensorRT upgrade. Off the hot path, best-effort.
        start_prebuild_sweep()

        self.get_logger().info(f"Training node ready — server={server_url} poll={poll_sec}s pub={pub_sec}s")

    def _register_local_skill_dirs(self) -> None:
        """Scan custom_skills/ and register every skill_id → local dir, so the
        status carries skill_dir for locally-recorded skills after a restart."""
        root = os.path.join(
            os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os")),
            "workspace",
            "custom_skills",
        )
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        n = 0
        for entry in entries:
            if not entry.is_dir():
                continue
            skill_id = read_skill_id(entry.path)
            if skill_id:
                self._store.register_dir(skill_id, entry.path)
                n += 1
        if n:
            self.get_logger().info(f"Registered {n} local skill dir(s) from {root}")

    def destroy_node(self) -> None:
        self._poller.stop()
        super().destroy_node()

    # ── Periodic publish ────────────────────────────────────────────

    def _build_skill_statuses(self) -> list:
        """Build the current list of TrainingSkillStatus from the store."""
        jobs, skills, transfers, completed, dir_map, ep_counts = self._store.snapshot()

        runs_by_skill: dict[str, list] = defaultdict(list)
        for run in jobs:
            runs_by_skill[run.skill_id].append(run)

        all_skill_ids = set(runs_by_skill.keys()) | set(skills.keys())
        result = []

        for sid in sorted(all_skill_ids):
            upload_xfer = transfers.get((TransferProgress.UPLOAD, sid, -1))
            # upload_done: in-memory completed set OR persisted ep count exists
            upload_done = (TransferProgress.UPLOAD, sid, -1) in completed or ep_counts.get(sid, -1) >= 0

            dl_xfers: dict[int, TransferProgress] = {}
            dl_done: set[int] = set()
            for run in runs_by_skill.get(sid, []):
                key = (TransferProgress.DOWNLOAD, sid, run.run_id)
                xfer = transfers.get(key)
                if xfer is not None:
                    dl_xfers[run.run_id] = xfer
                if key in completed:
                    dl_done.add(run.run_id)

            result.append(
                build_skill_status(
                    sid,
                    skills.get(sid),
                    runs_by_skill.get(sid, []),
                    upload_xfer,
                    upload_done,
                    dl_xfers,
                    dl_done,
                    skill_dir=dir_map.get(sid, ""),
                    uploaded_episode_count=ep_counts.get(sid, -1),
                )
            )
        return result

    def _publish(self) -> None:
        msg = TrainingJobList()
        msg.stamp = self.get_clock().now().to_msg()
        msg.skills = self._build_skill_statuses()
        self._pub.publish(msg)

    # ── Service: get_training_status ────────────────────────────────

    def _on_get_training_status(
        self, req: GetTrainingStatus.Request, res: GetTrainingStatus.Response
    ) -> GetTrainingStatus.Response:
        # Resolve skill_dir → skill_id from local metadata.json so we can
        # match even when the dir_map hasn't been populated by a prior
        # submit/create/download call.
        lookup_skill_id: str | None = None
        if req.skill_dir and os.path.isdir(req.skill_dir):
            lookup_skill_id = read_skill_id(req.skill_dir)
            if lookup_skill_id:
                self._store.register_dir(lookup_skill_id, req.skill_dir)
                if self._store.get_uploaded_ep_count(lookup_skill_id) < 0:
                    self._store.set_uploaded_ep_count(
                        lookup_skill_id,
                        _read_ep_count_from_disk(req.skill_dir),
                    )

        res.found = False
        for skill in self._build_skill_statuses():
            if (lookup_skill_id and skill.training_skill_id == lookup_skill_id) or (
                req.skill_name and skill.skill_name == req.skill_name
            ):
                res.found = True
                res.skill_status = skill
                break
        return res

    # ── Service: submit_skill ───────────────────────────────────────

    def _on_submit(self, req: SubmitSkill.Request, res: SubmitSkill.Response) -> SubmitSkill.Response:
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
                f"Skill {skill.skill_id[:8]} ({skill.name}) submitted — upload started from {req.skill_dir}"
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

    def _on_create_run(self, req: CreateRun.Request, res: CreateRun.Response) -> CreateRun.Response:
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
            data = self._mgr.client.create_run(skill_id, training_params=training_params)
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

    # ── Service: start_training ─────────────────────────────────────

    def _on_start_training(self, req: StartTraining.Request, res: StartTraining.Response) -> StartTraining.Response:
        """Kick off submit → upload → create_run on the robot and return at once.

        The whole chain runs on a background thread (`_run_training_flow`), so
        this service never blocks the single executor thread on network I/O —
        the dashboard and other services stay live while a (slow) submit/upload
        runs. The caller can disconnect immediately; the robot finishes the run.
        """
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res

        # Validate hyperparameters up front so bad input fails fast (sync, cheap).
        training_params, params_err = _build_training_params(req.run_params)
        if params_err:
            res.success, res.message = False, params_err
            return res

        # Dedup: the chain is slow, so the webapp/mobile may re-fire (or two
        # clients race). Without this each call would spawn its own upload +
        # create_run → duplicate, separately-billed GPU runs. The claim is held
        # until _run_training_flow finishes (released in its finally), so a
        # retry while one is in flight is a clean no-op.
        if not self._store.begin_start_training(req.skill_dir):
            res.success, res.message = False, "Training is already starting for this skill."
            return res

        # Persist the intent before any work so a crash/reboot mid-upload resumes
        # the run on next boot instead of silently dropping it.
        self._write_pending_marker(req.skill_dir, training_params)
        try:
            threading.Thread(
                target=self._run_training_flow,
                args=(req.skill_dir, training_params),
                daemon=True,
                name="train-start",
            ).start()
        except Exception as e:  # spawning a thread should never fail, but don't leak the claim
            self._clear_pending_marker(req.skill_dir)
            self._store.end_start_training(req.skill_dir)
            res.success, res.message = False, f"Couldn't start training: {e}"
            return res

        res.success = True
        res.message = "Training starting — uploading, then creating a run."
        self.get_logger().info(f"start_training: queued {req.skill_dir} (background)")
        return res

    def _run_training_flow(self, skill_dir: str, training_params: dict | None) -> None:
        """Background worker: submit → (idempotent) upload → create_run, off the
        executor thread. Surfaces a failure as a synthetic 'rejected' run so the
        dashboard shows it. Clears the resume marker the moment create_run
        succeeds (not only in the finally), so a kill after the run is created
        can't re-create a duplicate on the next boot; the finally still clears it
        for the upload-failed / create_run-failed / early-return paths.
        """
        skill_id: str | None = None
        try:
            gen = self._mgr.submit(skill_dir)
            skill: SkillInfo | None = None
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                skill = e.value
            if skill is None:
                self.get_logger().error(f"start_training: submit returned no skill for {skill_dir}")
                return
            skill_id = skill.skill_id
            self._store.put_skill(skill)
            self._store.register_dir(skill_id, skill_dir)
            self._store.clear_start_failure(skill_id)  # a fresh attempt clears the last failure marker
            self._store.mark_upload_pending(skill_id)

            if not do_upload(self._mgr, self._store, skill_id, skill_dir):
                self.get_logger().error(f"start_training: upload failed for {skill_id[:8]} — run not created")
                self._store.record_start_failure(skill_id, "Upload failed — run not created")
                return
            try:
                data = self._mgr.client.create_run(skill_id, training_params=training_params)
                # The run now exists on the cloud — clear the resume marker
                # immediately, *before* the run_status round-trip below. Otherwise a
                # crash/power-cut anywhere in that window would leave the marker and
                # re-create a duplicate, separately-billed run on the next boot.
                # (A residual single-call window remains; fully closing it would
                # need a cloud-side idempotency key on create_run.)
                self._clear_pending_marker(skill_dir)
                rid = int(data["run_id"])
                self._store.put_job(self._mgr.run_status(skill_id, rid))
                self._store.register_dir(skill_id, skill_dir)
                self.get_logger().info(f"start_training: run {skill_id[:8]}/{rid} created after upload")
            except Exception as e:
                self.get_logger().error(f"start_training: create_run failed for {skill_id[:8]}: {e}")
                self._store.record_start_failure(skill_id, f"Couldn't start training: {e}")
        except Exception as e:
            self.get_logger().error(f"start_training flow failed for {skill_dir}: {e}")
            if skill_id:
                self._store.record_start_failure(skill_id, f"Couldn't start training: {e}")
        finally:
            self._clear_pending_marker(skill_dir)
            self._store.end_start_training(skill_dir)

    # ── start_training resume markers ───────────────────────────────
    # A small on-disk record of an in-flight start_training, so a crash/reboot
    # between upload and create_run resumes the run instead of dropping it. Kept
    # outside the dataset dir (keyed by a hash of skill_dir) so it survives and
    # works for any skill_dir; the upload is idempotent, so a resumed run just
    # finishes the remainder.

    def _pending_dir(self) -> str:
        return os.path.join(
            os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os")),
            "workspace",
            ".training_pending",
        )

    def _marker_path(self, skill_dir: str) -> str:
        digest = hashlib.sha1(skill_dir.encode()).hexdigest()
        return os.path.join(self._pending_dir(), f"{digest}.json")

    def _write_pending_marker(self, skill_dir: str, training_params: dict | None) -> None:
        try:
            os.makedirs(self._pending_dir(), exist_ok=True)
            with open(self._marker_path(skill_dir), "w") as fh:
                json.dump({"skill_dir": skill_dir, "training_params": training_params}, fh)
        except OSError as e:
            # Best-effort: training still runs, we just lose crash-resume for it.
            self.get_logger().warning(f"start_training: couldn't write resume marker: {e}")

    def _clear_pending_marker(self, skill_dir: str) -> None:
        try:
            os.remove(self._marker_path(skill_dir))
        except OSError:
            pass

    def _resume_pending_training(self) -> None:
        try:
            files = [f for f in os.listdir(self._pending_dir()) if f.endswith(".json")]
        except OSError:
            return
        for name in files:
            path = os.path.join(self._pending_dir(), name)
            try:
                with open(path) as fh:
                    data = json.load(fh)
                skill_dir = data["skill_dir"]
                params = data.get("training_params")
            except (OSError, ValueError, KeyError):
                continue
            if not (skill_dir and os.path.isdir(skill_dir)):
                # Stale marker (skill deleted): drop it.
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            if not self._store.begin_start_training(skill_dir):
                continue
            self.get_logger().info(f"start_training: resuming interrupted run for {skill_dir}")
            threading.Thread(
                target=self._run_training_flow,
                args=(skill_dir, params),
                daemon=True,
                name="train-resume",
            ).start()

    # ── Service: cancel_run ─────────────────────────────────────────

    def _on_cancel_run(self, req: CancelRun.Request, res: CancelRun.Response) -> CancelRun.Response:
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res
        skill_id = read_skill_id(req.skill_dir)
        if not skill_id:
            res.success, res.message = False, f"No training_skill_id in {req.skill_dir}/metadata.json"
            return res
        try:
            self._mgr.client.update_run(skill_id, req.run_id, status="cancelled")
            # Reflect immediately so the dashboard updates before the next poll.
            self._store.put_job(self._mgr.run_status(skill_id, req.run_id))
            res.success = True
            res.message = f"Run {skill_id[:8]}/{req.run_id} cancelled"
            self.get_logger().info(res.message)
        except Exception as e:
            res.success, res.message = False, str(e)
            self.get_logger().error(f"cancel_run failed for {skill_id[:8]}/{req.run_id}: {e}")
        return res

    # ── Service: get_run_logs ───────────────────────────────────────

    def _on_get_run_logs(self, req: GetRunLogs.Request, res: GetRunLogs.Response) -> GetRunLogs.Response:
        """Fetch the live training-log tail for a run from the orchestrator.
        Best-effort: only populated while the run is actively training."""
        err = _require_absolute(req.skill_dir)
        if err:
            res.success, res.message = False, err
            return res
        skill_id = read_skill_id(req.skill_dir)
        if not skill_id:
            res.success, res.message = False, f"No training_skill_id in {req.skill_dir}/metadata.json"
            return res
        try:
            res.lines = self._mgr.client.get_run_logs(skill_id, req.run_id)
            res.success = True
            res.message = f"{len(res.lines)} log line(s)"
        except Exception as e:
            res.success, res.message = False, str(e)
            self.get_logger().warning(f"get_run_logs failed for {skill_id[:8]}/{req.run_id}: {e}")
        return res

    # ── Service: download_results ───────────────────────────────────

    def _on_download(self, req: DownloadResults.Request, res: DownloadResults.Response) -> DownloadResults.Response:
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
        self.get_logger().info(f"Download started for {skill_id}/{req.run_id} → {req.skill_dir}")
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
