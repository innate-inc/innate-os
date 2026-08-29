# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import time

from mars_msgs.srv import SetVolume

from innate import Skill, SkillReturn


class ModifySettings(Skill):
    """Set the robot speaker volume. `speaker_volume` must be an integer from
    0 to 100. The change applies immediately and persists across restarts."""

    def execute(self, speaker_volume: int) -> SkillReturn:
        volume = self._integer(speaker_volume, "speaker_volume", 0, 100)
        response = self._call(SetVolume, "/set_volume", SetVolume.Request(volume_percent=volume))
        if not response.success:
            self.fail(response.message or "The robot rejected the volume change.")
        return response.message or f"Speaker volume set to {volume}%"

    def _call(self, service_type, name: str, request, timeout: float = 5.0):
        if self.node is None:
            self.fail("Robot settings are unavailable: skill node is not running")
        client = self.node.create_client(service_type, name)
        if not client.wait_for_service(timeout_sec=2.0):
            self.fail(f"Robot settings service {name} is unavailable")
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done():
            if time.monotonic() >= deadline:
                self.fail(f"Robot settings service {name} timed out")
            self.sleep(0.02)
        try:
            response = future.result()
        except Exception as exc:
            self.fail(f"Robot settings service {name} failed: {exc}")
        if response is None:
            self.fail(f"Robot settings service {name} returned no response")
        return response

    def _integer(self, value, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            self.fail(f"{name} must be an integer from {minimum} to {maximum}")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            self.fail(f"{name} must be an integer from {minimum} to {maximum}")
        if str(value).strip() not in (str(parsed), f"{parsed}.0"):
            self.fail(f"{name} must be a whole number from {minimum} to {maximum}")
        if parsed < minimum or parsed > maximum:
            self.fail(f"{name} must be between {minimum} and {maximum}")
        return parsed
