# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import time
import uuid

from std_msgs.msg import String

from innate import Skill, SkillReturn

PROMPT_TOPIC = "/brain/express/prompt"
STATE_TOPIC = "/brain/express/state"
STOP_TOPIC = "/brain/express/stop"
START_TIMEOUT_S = 12.0  # the generated clip may come from the brain's own LLM, seconds after the stand-in
FINISH_GRACE_S = 3.0
STATE_STALE_S = 1.0  # the state topic runs at 5 Hz: older than this, the driver is gone
STAND_IN = "preset-stand-in"


class Express(Skill):
    """Act out a feeling or an attitude with the whole body — arm posture, gripper, head tilt and a
    small turn or step — from a short body-language prompt such as "proud, chest out", "a cat
    spotting a cucumber" or "sheepish, shrinking away". Use it when asked to show, act or mime
    something, or for a deliberate gesture longer than a reaction; quick reactions while talking
    belong in emote tags in your speech instead."""

    def execute(self, prompt: str, wait: bool = True) -> SkillReturn:
        prompt = " ".join(prompt.split())
        if not prompt:
            self.fail("Give a short body-language prompt, e.g. 'delighted, bouncing tall'")
        if self.node is None:
            self.fail("The expressive driver is unreachable: skill node is not running")

        state: dict = {}

        def on_state(msg: String) -> None:
            state.update(json.loads(msg.data), seen_at=time.monotonic())

        self.node.create_subscription(String, STATE_TOPIC, on_state, 10)
        prompts = self.node.create_publisher(String, PROMPT_TOPIC, 10)
        stop = self.node.create_publisher(String, STOP_TOPIC, 10)
        # A fresh publisher drops what it sends before the driver has matched it.
        self.wait_for(lambda: True if prompts.get_subscription_count() else None, timeout=1.0)
        request_id = uuid.uuid4().hex[:12]
        prompts.publish(String(data=json.dumps({"prompt": prompt, "id": request_id})))
        if not wait:
            return f"Expressing '{prompt}'"

        self.on_cancel(lambda: stop.publish(String()))
        # Done when a generated clip has played out; a stand-in alone is waited past, in case its
        # replacement is still on the way (none comes when generation fell back to the preset).
        played: dict = {}
        superseded = False  # a newer expression took the stage: the driver drops ours by itself
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            now = time.monotonic()
            current = dict(state)
            if now - current.get("seen_at", now) > STATE_STALE_S:
                break
            ours = current.get("id") == request_id
            if ours and current.get("playing"):
                played = current
                remaining = float(current.get("duration") or 0.0) - float(current.get("t") or 0.0)
                deadline = max(deadline, now + remaining + FINISH_GRACE_S)
            elif played and (played.get("source") != STAND_IN or current.get("playing")):
                superseded = not ours and bool(current.get("playing"))
                break
            self.sleep(0.1)
        if played.get("source") in (None, STAND_IN) and not superseded:
            stop.publish(String())  # the clip still being made must not play after this run gave up on it
        if not played:
            self.fail(f"The expression '{prompt}' never started (is expressive.enabled on?)")
        if played.get("source") == STAND_IN:
            return (
                f"Only a quick '{played.get('name')}' gesture played for '{prompt}': the full expression "
                f"was not ready within {START_TIMEOUT_S:.0f} s and was cancelled"
            )
        return f"Expressed '{prompt}' ({played.get('name')}, from the {played.get('source')})"
