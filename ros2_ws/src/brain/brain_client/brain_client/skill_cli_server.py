#!/usr/bin/env python3
"""Local Unix-socket bridge for low-latency skill CLI commands."""

import json
import os
import select
import socket
import threading
import time
from pathlib import Path

import rclpy
from brain_messages.action import ExecuteSkill
from rclpy.action import ActionClient
from rclpy.node import Node


DEFAULT_SOCKET_PATH = "/tmp/innate_skill_cli.sock"


class SkillCliServer(Node):
    def __init__(self):
        super().__init__("skill_cli_server")
        self._socket_path = Path(os.environ.get("INNATE_SKILL_SOCKET", DEFAULT_SOCKET_PATH))
        self._stop_event = threading.Event()
        self._server_socket = None
        self._action_client = ActionClient(self, ExecuteSkill, "execute_skill")
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()

    def _serve(self):
        try:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)

            server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_socket.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o666)
            server_socket.listen(8)
            server_socket.settimeout(0.2)
            self._server_socket = server_socket
            self.get_logger().info(f"Skill CLI socket listening at {self._socket_path}")
        except OSError as exc:
            self.get_logger().error(f"Failed to start skill CLI socket {self._socket_path}: {exc}")
            return

        while not self._stop_event.is_set():
            try:
                conn, _addr = server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn: socket.socket):
        with conn:
            request = self._read_request(conn)
            if request is None:
                return

            if request.get("action") != "run":
                self._send_event(conn, "error", message="Unsupported skill CLI action")
                return

            skill_type = str(request.get("skill_type") or "").strip()
            if not skill_type:
                self._send_event(conn, "error", message="Missing skill_type")
                return

            try:
                inputs_json = self._normalize_inputs(request.get("inputs", "{}"))
                timeout = self._optional_float(request.get("timeout"))
                server_timeout = self._optional_float(request.get("server_timeout")) or 5.0
            except ValueError as exc:
                self._send_event(conn, "error", message=str(exc))
                return

            self._run_skill(conn, skill_type, inputs_json, timeout, server_timeout)

    def _read_request(self, conn: socket.socket):
        conn.settimeout(1.0)
        data = b""
        try:
            while b"\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                data += chunk
        except OSError:
            return None

        line = data.split(b"\n", 1)[0]
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_event(conn, "error", message="Invalid JSON request")
            return None
        return request if isinstance(request, dict) else None

    def _normalize_inputs(self, raw_inputs) -> str:
        if isinstance(raw_inputs, dict):
            return json.dumps(raw_inputs, separators=(",", ":"))
        if not isinstance(raw_inputs, str):
            raise ValueError("inputs must be a JSON object")
        try:
            parsed = json.loads(raw_inputs)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid inputs JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("inputs must be a JSON object")
        return json.dumps(parsed, separators=(",", ":"))

    def _optional_float(self, value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected a number, got {value!r}") from exc

    def _run_skill(self, conn: socket.socket, skill_type: str, inputs_json: str,
                   timeout: float | None, server_timeout: float):
        if not self._action_client.wait_for_server(timeout_sec=server_timeout):
            self._send_event(conn, "error", message="ExecuteSkill action server not available")
            return

        goal = ExecuteSkill.Goal()
        goal.skill_type = skill_type
        goal.inputs = inputs_json

        def feedback_callback(feedback_msg):
            feedback = feedback_msg.feedback
            text = getattr(feedback, "feedback", "")
            if text:
                self._send_event(conn, "feedback", text=text)

        send_goal_future = self._action_client.send_goal_async(goal, feedback_callback=feedback_callback)
        wait_state = self._wait_future_or_disconnect(conn, send_goal_future, server_timeout)
        if wait_state == "disconnected":
            return
        if wait_state != "done":
            self._send_event(conn, "error", message="Timed out waiting for skill goal acceptance")
            return

        goal_handle = send_goal_future.result()
        if not goal_handle.accepted:
            self._send_event(conn, "error", message="Skill goal was rejected")
            return

        result_future = goal_handle.get_result_async()
        wait_state = self._wait_future_or_disconnect(conn, result_future, timeout)
        if wait_state == "disconnected":
            self._cancel_goal(goal_handle)
            return
        if wait_state != "done":
            self._cancel_goal(goal_handle)
            self._send_event(conn, "result", success=False, message="Timed out waiting for skill result")
            return

        result = result_future.result().result
        self._send_event(
            conn,
            "result",
            success=bool(result.success),
            message=result.message,
            skill_type=result.skill_type,
            success_type=result.success_type,
        )

    def _wait_future_or_disconnect(self, conn: socket.socket, future, timeout: float | None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while rclpy.ok() and not future.done():
            if not self._client_connected(conn):
                return "disconnected"
            if deadline is not None and time.monotonic() >= deadline:
                return "timeout"
            time.sleep(0.01)
        return "done" if future.done() else "shutdown"

    def _client_connected(self, conn: socket.socket) -> bool:
        try:
            readable, _, _ = select.select([conn], [], [], 0)
            if not readable:
                return True
            return bool(conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT))
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _cancel_goal(self, goal_handle):
        cancel_future = goal_handle.cancel_goal_async()
        self._wait_plain_future(cancel_future, 2.0)

    def _wait_plain_future(self, future, timeout: float):
        deadline = time.monotonic() + timeout
        while rclpy.ok() and not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        return future.done()

    def _send_event(self, conn: socket.socket, event: str, **payload) -> bool:
        payload["event"] = event
        try:
            conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def destroy(self):
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
        self._server_thread.join(timeout=1.0)
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._action_client.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = SkillCliServer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
