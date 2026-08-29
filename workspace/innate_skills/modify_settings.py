# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import time
import urllib.error
import urllib.request
from typing import Literal, cast

from mars_msgs.srv import SetVolume
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import SetParameters
from std_srvs.srv import SetBool

from innate import Skill, SkillReturn

SettingName = Literal["speaker_volume", "microphone_enabled", "driving_sound", "tts_voice"]
VALID_SETTINGS = ("speaker_volume", "microphone_enabled", "driving_sound", "tts_voice")
SETTINGS_ENDPOINTS = ("http://127.0.0.1:4080/settings.json", "http://127.0.0.1/settings.json")


class ModifySettings(Skill):
    """Change a safe user-facing robot setting. Provide `setting` and `value`.

    Supported settings:
    - `speaker_volume`: integer 0-100.
    - `microphone_enabled`: true or false.
    - `driving_sound`: a motor-sound name offered by this robot.
    - `tts_voice`: a Cartesia voice ID.

    Speaker volume and microphone changes apply immediately and persist. Driving
    sound and TTS voice also persist in Settings and apply to the running robot.
    Do not use this skill to alter motion or hardware-safety limits."""

    def execute(self, setting: SettingName, value: str | int | float | bool) -> SkillReturn:
        setting = cast(SettingName, str(setting).strip().lower())
        if setting not in VALID_SETTINGS:
            self.fail(f"Unknown setting '{setting}'. Available: {', '.join(VALID_SETTINGS)}")

        if setting == "speaker_volume":
            volume = self._integer(value, setting, 0, 100)
            response = self._call(SetVolume, "/set_volume", SetVolume.Request(volume_percent=volume))
            if not response.success:
                self.fail(response.message or "The robot rejected the volume change.")
            return response.message or f"Speaker volume set to {volume}%"

        if setting == "microphone_enabled":
            enabled = self._boolean(value, setting)
            response = self._call(SetBool, "/set_microphone", SetBool.Request(data=enabled))
            if not response.success:
                self.fail(response.message or "The robot rejected the microphone change.")
            return response.message or f"Microphone {'enabled' if enabled else 'disabled'}"

        text = str(value).strip()
        if not text:
            self.fail(f"{setting} cannot be empty")
        if setting == "driving_sound":
            path = ["motor_sound", "ros__parameters", "motor_sound", "voice"]
            node, parameter = "/motor_sound", "motor_sound.voice"
        else:
            path = ["/**", "ros__parameters", "cartesia_voice_id"]
            node, parameter = "/brain_client_node", "cartesia_voice_id"

        self._persist(path, text)
        response = self._call(
            SetParameters,
            f"{node}/set_parameters",
            SetParameters.Request(
                parameters=[
                    Parameter(
                        name=parameter,
                        value=ParameterValue(type=ParameterType.PARAMETER_STRING, string_value=text),
                    )
                ]
            ),
        )
        result = response.results[0] if response.results else None
        if result is None or not result.successful:
            reason = result.reason if result is not None else "no response result"
            self.fail(f"Saved {setting}, but the running robot did not apply it: {reason}")
        return f"{setting.replace('_', ' ').capitalize()} set to {text}"

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

    def _persist(self, path: list[str], value: str) -> None:
        payload = json.dumps({"sets": [{"path": path, "value": value, "type": "string"}], "clears": []}).encode()
        last_error = "settings endpoint unavailable"
        for endpoint in SETTINGS_ENDPOINTS:
            request = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3.0) as response:
                    body = json.loads(response.read())
                if body.get("ok"):
                    return
                last_error = body.get("message") or "settings update was rejected"
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last_error = str(exc)
        self.fail(f"Could not save {'.'.join(path)}: {last_error}")

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

    def _boolean(self, value, name: str) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in ("true", "on", "yes", "1", "enabled"):
            return True
        if normalized in ("false", "off", "no", "0", "disabled"):
            return False
        self.fail(f"{name} must be true or false")
