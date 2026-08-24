# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import base64
import binascii
import io
import subprocess
import threading
import wave
from collections.abc import Callable
from importlib import import_module
from tempfile import NamedTemporaryFile
from typing import Protocol, cast

from innate import Skill, SkillReturn, resource

StringMessage = import_module("std_msgs.msg").String

MIC_CAPTURE_TOPIC = "/mic/capture"
TTS_STATUS_TOPIC = "/tts/is_playing"
SAMPLE_RATE = 24_000
MIN_RECORD_SECONDS = 1.0
MAX_RECORD_SECONDS = 15.0


class _StringMessage(Protocol):
    data: str


class _Publisher(Protocol):
    def get_subscription_count(self) -> int: ...

    def publish(self, msg: _StringMessage) -> None: ...


class _RosNode(Protocol):
    def create_subscription(
        self,
        msg_type: type[_StringMessage],
        topic: str,
        callback: Callable[[_StringMessage], None],
        depth: int,
    ) -> object: ...

    def create_publisher(self, msg_type: type[_StringMessage], topic: str, depth: int) -> _Publisher: ...


class _PcmRecorder:
    def __init__(self, node: _RosNode) -> None:
        self._chunks: list[bytes] = []
        self._recording = False
        self._lock = threading.Lock()
        self._subscription = node.create_subscription(StringMessage, MIC_CAPTURE_TOPIC, self._on_audio, 10)

    def _on_audio(self, msg: _StringMessage) -> None:
        try:
            chunk = base64.b64decode(msg.data, validate=True)
        except (binascii.Error, ValueError):
            return
        if not chunk or len(chunk) % 2:
            return
        with self._lock:
            if self._recording:
                self._chunks.append(chunk)

    def start(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._recording = True

    def stop(self) -> bytes:
        with self._lock:
            self._recording = False
            return b"".join(self._chunks)


class Playback(Skill):
    """Record a short microphone clip and play back exactly what Mars heard."""

    @resource
    def recorder(self) -> _PcmRecorder:
        return _PcmRecorder(self._ros_node())

    def guidelines(self) -> str:
        return (
            "Use on a physical robot when the user asks Mars to record them, echo the microphone, "
            "or play back what it hears. The optional record_seconds controls the recording length."
        )

    def execute(self, record_seconds: float = 5.0) -> SkillReturn:
        if not MIN_RECORD_SECONDS <= record_seconds <= MAX_RECORD_SECONDS:
            self.fail(f"record_seconds must be between {MIN_RECORD_SECONDS:g} and {MAX_RECORD_SECONDS:g}")
        if not self._speaker_available():
            self.fail("Playback requires a physical robot with an audio output")

        recorder = self.recorder
        self.say("I want you to talk.", wait=True)
        recorder.start()
        try:
            self.sleep(record_seconds)
        finally:
            pcm = recorder.stop()

        if not pcm:
            self.fail("No microphone audio was captured")

        wav = self._to_wav(pcm)
        self._play_on_robot(wav)
        return f"Recorded and played back {record_seconds:g} seconds of microphone audio"

    @staticmethod
    def _to_wav(pcm: bytes) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(SAMPLE_RATE)
            clip.writeframes(pcm)
        return output.getvalue()

    @staticmethod
    def _speaker_available() -> bool:
        try:
            result = subprocess.run(
                ["aplay", "-l"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and "card " in result.stdout

    def _play_on_robot(self, wav: bytes) -> None:
        status = self._ros_node().create_publisher(StringMessage, TTS_STATUS_TOPIC, 10)
        self.wait_for(lambda: True if status.get_subscription_count() else None, timeout=1.0)
        with NamedTemporaryFile(suffix=".wav") as clip:
            clip.write(wav)
            clip.flush()
            player = subprocess.Popen(
                ["aplay", "-q", clip.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self.on_cancel(player.terminate)
            status.publish(StringMessage(data="true"))
            try:
                while player.poll() is None:
                    self.sleep(0.05)
            finally:
                status.publish(StringMessage(data="false"))

        if player.returncode == 0:
            return
        stderr = player.stderr.read().decode(errors="replace").strip() if player.stderr else ""
        detail = stderr or f"aplay exited with {player.returncode}"
        self.fail(f"Audio playback failed: {detail}")

    def _ros_node(self) -> _RosNode:
        if self.node is None:
            self.fail("Playback is not connected to ROS")
        return cast(_RosNode, self.node)
