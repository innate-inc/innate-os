# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import time
import uuid

from std_msgs.msg import Empty, String

from innate import Skill, SkillReturn

PROMPT_TOPIC = "/brain/express/prompt"
STATE_TOPIC = "/brain/express/state"
STOP_TOPIC = "/brain/express/stop"
START_TIMEOUT_S = 12.0  # the prompt may fall back to the brain's own LLM before a clip exists
FINISH_GRACE_S = 3.0


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
            state.update(json.loads(msg.data))

        self.node.create_subscription(String, STATE_TOPIC, on_state, 10)
        prompts = self.node.create_publisher(String, PROMPT_TOPIC, 10)
        stop = self.node.create_publisher(Empty, STOP_TOPIC, 10)
        # A fresh publisher drops what it sends before the driver has matched it.
        self.wait_for(lambda: True if prompts.get_subscription_count() else None, timeout=1.0)
        request_id = uuid.uuid4().hex[:12]
        prompts.publish(String(data=json.dumps({"prompt": prompt, "id": request_id})))
        if not wait:
            return f"Expressing '{prompt}'"

        self.on_cancel(lambda: stop.publish(Empty()))
        started = self.wait_for(
            lambda: dict(state) if state.get("id") == request_id and state.get("playing") else None,
            timeout=START_TIMEOUT_S,
        )
        if started is None:
            self.fail(f"The expression '{prompt}' never started (is expressive.enabled on?)")
        deadline = time.monotonic() + float(started.get("duration") or 0.0) + FINISH_GRACE_S
        while state.get("id") == request_id and state.get("playing") and time.monotonic() < deadline:
            self.sleep(0.1)
        return f"Expressed '{prompt}' ({started.get('name')}, from the {started.get('source')})"
