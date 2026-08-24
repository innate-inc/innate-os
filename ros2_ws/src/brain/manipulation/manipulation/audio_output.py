# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import base64
import io
import json
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, final

from rclpy.node import Node
from std_msgs.msg import String

_BROWSER_TIMEOUT_ERROR = "Browser audio playback timed out"


@dataclass
class _Playback:
    clip_id: str
    process: subprocess.Popen[bytes] | None = None
    deadline: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None


@final
class AudioOutput:
    """Plays WAV cues through ALSA on hardware or the browser in simulation."""

    def __init__(self, node: Node, simulator_mode: bool) -> None:
        self._simulator_mode = simulator_mode
        self._status_pub = node.create_publisher(String, "/tts/is_playing", 10)
        self._audio_pub = node.create_publisher(String, "/tts/audio", 10)
        self._cancel_pub = node.create_publisher(String, "/tts/cancel", 10)
        self._active: dict[str, _Playback] = {}
        self._lock = threading.Lock()
        self._playback_sub = (
            node.create_subscription(String, "/tts/playback", self._on_browser_playback, 10) if simulator_mode else None
        )

    @property
    def playing(self) -> bool:
        with self._lock:
            return bool(self._active)

    def start(self, path: str) -> None:
        clip_id = uuid.uuid4().hex
        playback = _Playback(clip_id)
        audio_request: str | None = None

        if self._simulator_mode:
            wav = Path(path).read_bytes()
            playback.deadline = time.monotonic() + max(15.0, _wav_duration(wav) + 10.0)
            audio_request = json.dumps(
                {
                    "id": clip_id,
                    "audio": base64.b64encode(wav).decode("ascii"),
                    "near_end_lead_seconds": 0,
                },
                separators=(",", ":"),
            )
        else:
            playback.process = subprocess.Popen(
                ["aplay", "-q", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        with self._lock:
            was_silent = not self._active
            self._active[clip_id] = playback
        if was_silent:
            self._status_pub.publish(String(data="true"))
        if audio_request is not None:
            self._audio_pub.publish(String(data=audio_request))

    def poll(self) -> str | None:
        errors: list[str] = []
        timed_out: list[str] = []
        with self._lock:
            had_active = bool(self._active)
            for clip_id, playback in tuple(self._active.items()):
                self._poll_process(playback)
                if not playback.done.is_set():
                    continue
                _ = self._active.pop(clip_id)
                if playback.error:
                    errors.append(playback.error)
                if playback.error == _BROWSER_TIMEOUT_ERROR:
                    timed_out.append(clip_id)
            stopped = had_active and not self._active
        for clip_id in timed_out:
            self._cancel_pub.publish(String(data=clip_id))
        if stopped:
            self._status_pub.publish(String(data="false"))
        return errors[0] if errors else None

    def stop(self) -> None:
        with self._lock:
            playbacks = tuple(self._active.values())
            self._active.clear()
        for playback in playbacks:
            if playback.process is None:
                self._cancel_pub.publish(String(data=playback.clip_id))
                continue
            self._terminate(playback.process)
        if playbacks:
            self._status_pub.publish(String(data="false"))

    def _on_browser_playback(self, msg: String) -> None:
        try:
            raw_payload = cast(object, json.loads(cast(str, msg.data)))
            if not isinstance(raw_payload, dict):
                return
            payload = cast(dict[str, object], raw_payload)
            clip_id = payload["id"]
            event = payload["event"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return
        if not isinstance(clip_id, str) or not isinstance(event, str):
            return
        if event not in {"ended", "aborted"}:
            return
        with self._lock:
            playback = self._active.get(clip_id)
            if playback is None:
                return
            if event == "aborted":
                playback.error = "Browser audio playback aborted"
            playback.done.set()

    @staticmethod
    def _poll_process(playback: _Playback) -> None:
        process = playback.process
        if process is None:
            if playback.deadline is not None and time.monotonic() >= playback.deadline:
                playback.error = _BROWSER_TIMEOUT_ERROR
                playback.done.set()
            return
        if process.poll() is None:
            return
        if process.returncode != 0:
            stderr_bytes = cast(bytes, process.stderr.read()) if process.stderr else b""
            stderr = stderr_bytes.decode(errors="replace").strip()
            playback.error = f"Audio playback failed: {stderr or f'aplay exited with {process.returncode}'}"
        playback.done.set()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            _ = process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait()


def _wav_duration(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as clip:
        return clip.getnframes() / clip.getframerate()
