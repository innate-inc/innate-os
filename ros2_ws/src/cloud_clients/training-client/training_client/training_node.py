#!/usr/bin/env python3
"""
ROS 2 Humble lifecycle node for the Innate Training Client.

Services:
  ~/submit_skill        (std_srvs/srv/Trigger + params)  — create skill, upload, create run
  ~/download_results    (std_srvs/srv/Trigger + param)   — download results for a run

Topics (published):
  ~/progress            (std_msgs/msg/String)   — JSON progress updates
  ~/status              (std_msgs/msg/String)   — JSON run status snapshots

Parameters:
  server_url            (string)   — orchestrator URL
  auth_token            (string)   — user bearer token
  poll_interval         (double)   — status poll interval in seconds (default 20.0)
  source_dir            (string)   — default source directory for submissions
  skill_name            (string)   — optional skill name (default: directory name)
  repo                  (string)   — default GitHub repo (owner/repo)
  ref                   (string)   — default git ref (default "main")
  command               (string[]) — default training command

This node requires rclpy (ROS 2 Humble). It is NOT imported by the rest of the
training_client library, so the core library works without ROS installed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict

from collections.abc import Generator

import rclpy
from rclpy.lifecycle import Node as LifecycleNode
from rclpy.lifecycle import State, TransitionCallbackReturn
from std_msgs.msg import String
from std_srvs.srv import Trigger

from typing import Any

from training_client.src.skill_manager import SkillManager
from training_client.src.types import ClientConfig, ProgressUpdate, RunInfo


class TrainingNode(LifecycleNode):
    """ROS 2 lifecycle node for training skill/run management."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("training_client", **kwargs)

        # Declare parameters
        self.declare_parameter("server_url", "")
        self.declare_parameter("auth_token", "")
        self.declare_parameter("auth_issuer_url", "")
        self.declare_parameter("poll_interval", 20.0)
        self.declare_parameter("source_dir", "")
        self.declare_parameter("skill_name", "")
        self.declare_parameter("repo", "")
        self.declare_parameter("ref", "main")
        self.declare_parameter("command", [""])

        self._manager: SkillManager | None = None
        self._active_skill_id: str | None = None
        self._active_run_id: int | None = None
        self._worker_thread: threading.Thread | None = None

    # ── Lifecycle callbacks ─────────────────────────────────────────

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        server_url = self.get_parameter("server_url").get_parameter_value().string_value
        auth_token = self.get_parameter("auth_token").get_parameter_value().string_value
        auth_issuer_url = (
            self.get_parameter("auth_issuer_url").get_parameter_value().string_value
        )
        poll_interval = (
            self.get_parameter("poll_interval").get_parameter_value().double_value
        )

        if not server_url or not auth_token:
            self.get_logger().error("server_url and auth_token parameters are required")
            return TransitionCallbackReturn.FAILURE

        config = ClientConfig(
            server_url=server_url,
            auth_token=auth_token,
            auth_issuer_url=auth_issuer_url,
            poll_interval_seconds=poll_interval,
        )
        self._manager = SkillManager(config)

        # Publishers
        self._progress_pub = self.create_publisher(String, "~/progress", 10)
        self._status_pub = self.create_publisher(String, "~/status", 10)

        # Services
        self._submit_srv = self.create_service(
            Trigger, "~/submit_skill", self._handle_submit
        )
        self._download_srv = self.create_service(
            Trigger, "~/download_results", self._handle_download
        )

        self.get_logger().info("Configured with server: %s" % server_url)
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        self.get_logger().info("Activated — ready to accept skill requests")
        return TransitionCallbackReturn.SUCCESS

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        self.get_logger().info("Deactivated")
        return TransitionCallbackReturn.SUCCESS

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        self._manager = None
        return TransitionCallbackReturn.SUCCESS

    # ── Service handlers ────────────────────────────────────────────

    def _handle_submit(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self._worker_thread and self._worker_thread.is_alive():
            response.success = False
            response.message = "An operation is already in progress"
            return response

        source_dir = self.get_parameter("source_dir").get_parameter_value().string_value
        if not source_dir:
            response.success = False
            response.message = "source_dir parameter not set"
            return response

        skill_name = (
            self.get_parameter("skill_name").get_parameter_value().string_value or None
        )
        repo = self.get_parameter("repo").get_parameter_value().string_value
        ref = self.get_parameter("ref").get_parameter_value().string_value
        command = self.get_parameter("command").get_parameter_value().string_array_value

        params = {}
        if repo:
            params["repo"] = repo
        if ref:
            params["ref"] = ref
        if command and command != [""]:
            params["command"] = list(command)

        self._worker_thread = threading.Thread(
            target=self._run_submit,
            args=(source_dir, skill_name, params),
            daemon=True,
        )
        self._worker_thread.start()

        response.success = True
        response.message = f"Skill submission started from {source_dir}"
        return response

    def _handle_download(
        self, request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if not self._active_skill_id or self._active_run_id is None:
            response.success = False
            response.message = "No active run to download results for"
            return response

        if self._worker_thread and self._worker_thread.is_alive():
            response.success = False
            response.message = "An operation is already in progress"
            return response

        self._worker_thread = threading.Thread(
            target=self._run_download,
            args=(self._active_skill_id, self._active_run_id),
            daemon=True,
        )
        self._worker_thread.start()

        response.success = True
        response.message = (
            f"Download started for run {self._active_skill_id}/{self._active_run_id}"
        )
        return response

    # ── Worker threads ──────────────────────────────────────────────

    def _run_submit(
        self, source_dir: str, skill_name: str | None, params: dict[str, Any]
    ) -> None:
        try:
            gen = self._manager.submit(source_dir, name=skill_name)
            final_update = self._consume_progress(gen)

            if (
                final_update
                and hasattr(final_update, "skill_id")
                and final_update.skill_id
            ):
                self._active_skill_id = final_update.skill_id

                # Create a run with training params
                run_resp = self._manager.client.create_run(
                    final_update.skill_id,
                    training_params=params or None,
                )
                self._active_run_id = run_resp["run_id"]

                # Start watching
                self._run_watch(final_update.skill_id, self._active_run_id)
        except Exception as e:
            self.get_logger().error(f"Submit failed: {e}")
            self._publish_progress_msg("error", str(e))

    def _run_watch(self, skill_id: str, run_id: int) -> None:
        try:
            gen = self._manager.watch(skill_id, run_id)
            for update in gen:
                self._publish_progress(update)
                # Also publish as status
                run_info = self._manager.run_status(skill_id, run_id)
                self._publish_status(run_info)
        except Exception as e:
            self.get_logger().error(f"Watch failed: {e}")

    def _run_download(self, skill_id: str, run_id: int) -> None:
        try:
            gen = self._manager.download(skill_id, run_id)
            self._consume_progress(gen)
        except Exception as e:
            self.get_logger().error(f"Download failed: {e}")
            self._publish_progress_msg("error", str(e))

    # ── Publishing helpers ──────────────────────────────────────────

    def _consume_progress(
        self, gen: Generator[ProgressUpdate, None, None]
    ) -> ProgressUpdate | None:
        """Iterate a progress generator, publishing each update. Return final value."""
        result: ProgressUpdate | None = None
        try:
            for update in gen:
                self._publish_progress(update)
                result = update
        except StopIteration as e:
            result = e.value
        return result

    def _publish_progress(self, update: ProgressUpdate) -> None:
        msg = String()
        data = {
            "stage": update.stage.value,
            "message": update.message,
            "skill_id": update.skill_id,
            "run_id": update.run_id,
            "error": update.error,
        }
        if update.file_progress:
            data["file_progress"] = asdict(update.file_progress)
        msg.data = json.dumps(data)
        self._progress_pub.publish(msg)

    def _publish_status(self, run_info: RunInfo) -> None:
        msg = String()
        data = {
            "skill_id": run_info.skill_id,
            "run_id": run_info.run_id,
            "status": run_info.status,
            "daemon_state": run_info.daemon_state,
            "error_message": run_info.error_message,
            "updated_at": run_info.updated_at,
        }
        msg.data = json.dumps(data)
        self._status_pub.publish(msg)

    def _publish_progress_msg(self, stage: str, message: str) -> None:
        msg = String()
        msg.data = json.dumps({"stage": stage, "message": message})
        self._progress_pub.publish(msg)


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
