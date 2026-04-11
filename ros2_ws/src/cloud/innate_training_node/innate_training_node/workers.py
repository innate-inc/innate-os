"""Background workers: polling, upload, download.

All functions in this module run on daemon threads and communicate with
the rest of the node exclusively through :class:`JobStore` and the
``rclpy`` logger.

Downloads automatically activate the run (set checkpoint + stats in
``metadata.json``) once the transfer completes.
"""

from __future__ import annotations

import threading
import time

import rclpy.logging
from innate_cloud_msgs.msg import TransferProgress

from training_client.src.skill_manager import SkillManager
from training_client.src.types import ProgressStage, ProgressUpdate

from .job_store import JobStore

_ros = rclpy.logging.get_logger("innate_training")


class Poller:
    """Periodically fetches all jobs, polls active runs, and starts uploads."""

    def __init__(
        self,
        manager: SkillManager,
        store: JobStore,
        poll_interval: float,
    ) -> None:
        self._mgr = manager
        self._store = store
        self._interval = poll_interval
        self._shutting_down = False

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="training_poll").start()

    def stop(self) -> None:
        self._shutting_down = True

    # ── Main loop ───────────────────────────────────────────────────

    def _loop(self) -> None:
        try:
            self._fetch_all()
        except Exception as e:
            _ros.error(f"Initial fetch failed: {e}")

        while not self._shutting_down:
            try:
                self._poll_active()
            except Exception as e:
                _ros.warning(f"Poll cycle error: {e}")
            time.sleep(self._interval)

    def _fetch_all(self) -> None:
        _ros.info("Fetching all skills and runs from server…")
        try:
            skills = self._mgr.list_skills()
        except Exception as e:
            _ros.error(f"list_skills failed: {e}")
            return

        for skill in skills:
            self._store.put_skill(skill)
        _ros.info(f"Fetched {len(skills)} skill(s)")

        total_runs = 0
        done_runs = 0
        for skill in skills:
            try:
                runs = self._mgr.list_runs(skill.skill_id)
                for run in runs:
                    total_runs += 1
                    self._store.put_job(run)
                    _ros.debug(
                        f"  skill={skill.name or skill.skill_id[:8]} "
                        f"run={run.run_id} status={run.status}"
                        f"{' daemon=' + run.daemon_state if run.daemon_state else ''}"
                    )
                    if run.status == "done":
                        done_runs += 1
                        dest = self._store.dir_for_skill(run.skill_id)
                        if dest:
                            maybe_auto_download(
                                self._mgr,
                                self._store,
                                run.skill_id,
                                run.run_id,
                                dest,
                            )
            except Exception as e:
                _ros.warning(f"list_runs({skill.skill_id[:8]}) failed: {e}")

        _ros.info(
            f"Startup fetch complete: {len(skills)} skill(s), "
            f"{total_runs} run(s) ({done_runs} done)"
        )

    def _poll_active(self) -> None:
        for (skill_id, run_id), old in self._store.active_jobs():
            if self._shutting_down:
                return
            sid = skill_id[:8]
            try:
                new = self._mgr.run_status(skill_id, run_id)
                self._store.put_job(new)

                status_changed = new.status != old.status
                daemon_changed = new.daemon_state != old.daemon_state

                if status_changed or daemon_changed:
                    parts = [f"Run {sid}/{run_id}: {old.status}→{new.status}"]
                    if new.daemon_state:
                        parts.append(f"daemon={new.daemon_state}")
                    if new.instance_type and new.status in ("booting", "running"):
                        parts.append(f"instance={new.instance_type}")
                    if new.instance_ip and new.status == "running":
                        parts.append(f"ip={new.instance_ip}")
                    _ros.info(" | ".join(parts))

                    if new.error_message:
                        _ros.warning(
                            f"Run {sid}/{run_id} error: {new.error_message}"
                        )

                if new.status == "done" and old.status != "done":
                    _ros.info(f"Run {sid}/{run_id} done → auto-download")
                    dest = self._store.dir_for_skill(skill_id)
                    if dest:
                        maybe_auto_download(
                            self._mgr,
                            self._store,
                            skill_id,
                            run_id,
                            dest,
                        )
                    else:
                        _ros.warning(
                            f"No skill dir known for {sid} — skipping auto-download"
                        )
            except Exception as e:
                _ros.warning(f"Poll {sid}/{run_id} failed: {e}")


# ── Upload worker ───────────────────────────────────────────────────


def do_upload(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    skill_dir: str,
) -> None:
    """Upload data files for *skill_id*."""
    sid = skill_id[:8]
    prev_stage: str | None = None
    prev_msg: str | None = None
    try:
        for update in manager.upload_files(skill_id, skill_dir):
            store.update_transfer(TransferProgress.UPLOAD, skill_id, -1, update)
            stage = update.stage.value
            if stage != prev_stage or update.message != prev_msg:
                _ros.info(f"Upload {sid}: {stage} — {update.message}")
                prev_stage = stage
                prev_msg = update.message
        _ros.info(f"Upload finished for {sid} from {skill_dir}")
    except Exception as e:
        _ros.error(f"Upload failed for {sid}: {e}")
        store.update_transfer(
            TransferProgress.UPLOAD,
            skill_id,
            -1,
            ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"Upload failed: {e}",
                skill_id=skill_id,
                error=str(e),
            ),
        )
        return

    store.update_transfer(
        TransferProgress.UPLOAD,
        skill_id,
        -1,
        ProgressUpdate(
            stage=ProgressStage.DONE,
            message="Upload complete",
            skill_id=skill_id,
        ),
    )


# ── Download worker ─────────────────────────────────────────────────


def do_download(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    run_id: int,
    dest_dir: str,
) -> None:
    """Download & decompress result files for a run."""
    sid = skill_id[:8]
    prev_stage: str | None = None
    t0 = time.monotonic()
    try:
        for update in manager.download(skill_id, run_id, dest_dir=dest_dir):
            store.update_transfer(TransferProgress.DOWNLOAD, skill_id, run_id, update)
            stage = update.stage.value
            if stage != prev_stage:
                _ros.info(f"Download {sid}/{run_id}: {stage} — {update.message}")
                prev_stage = stage
            fp = update.file_progress
            if fp and fp.bytes_done == 0 and not fp.done:
                _ros.debug(
                    f"Download {sid}/{run_id}: file [{fp.index}/{fp.total}] "
                    f"{fp.filename} ({fp.bytes_total} bytes)"
                )
        # Clear finished transfer entry.
        store.update_transfer(
            TransferProgress.DOWNLOAD,
            skill_id,
            run_id,
            ProgressUpdate(
                stage=ProgressStage.DONE,
                message="Download complete",
                skill_id=skill_id,
                run_id=run_id,
            ),
        )
        # Refresh cached state (download marks the run as "downloaded").
        try:
            store.put_job(manager.run_status(skill_id, run_id))
        except Exception:
            pass

        # Activate: set checkpoint + stats_file in metadata.json.
        try:
            result = manager.activate_run(dest_dir, run_id)
            _ros.info(
                f"Activated run {sid}/{run_id}: "
                f"checkpoint={result['checkpoint']} stats_file={result['stats_file']}"
            )
        except Exception as e:
            _ros.warning(
                f"Download OK but activation failed for {sid}/{run_id}: {e}"
            )

        elapsed = time.monotonic() - t0
        _ros.info(
            f"Download finished for {sid}/{run_id} → {dest_dir} "
            f"({elapsed:.1f}s)"
        )
    except Exception as e:
        _ros.error(f"Download failed for {sid}/{run_id}: {e}")
        store.update_transfer(
            TransferProgress.DOWNLOAD,
            skill_id,
            run_id,
            ProgressUpdate(
                stage=ProgressStage.ERROR,
                message=f"Download failed: {e}",
                skill_id=skill_id,
                run_id=run_id,
                error=str(e),
            ),
        )


# ── Auto-download helper ───────────────────────────────────────────


def maybe_auto_download(
    manager: SkillManager,
    store: JobStore,
    skill_id: str,
    run_id: int,
    download_dir: str,
) -> None:
    """Spawn a download thread if one hasn't been started already."""
    if not store.mark_download_started(skill_id, run_id):
        return

    threading.Thread(
        target=do_download,
        args=(manager, store, skill_id, run_id, download_dir),
        daemon=True,
        name=f"dl-{skill_id[:8]}-{run_id}",
    ).start()
