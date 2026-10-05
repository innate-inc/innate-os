# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.mapped_sock_demo import MappedSockDemo

from innate import Agent


class MappedSockAgent(Agent):
    """Three mapped socks, one stationary base, no microphone."""

    model = "google:gemini-3.6-flash"
    model_extra_body = "{}"
    minimal_system_prompt = True
    history_max_entries = 12
    history_max_image_turns = 0
    supervision_turn_interval = 60.0
    idle_turn_interval = 60.0

    @property
    def id(self):
        return "mapped_sock_agent"

    @property
    def display_name(self):
        return "Mapped Three-Sock Demo"

    def get_skills(self):
        return [MappedSockDemo]

    def get_inputs(self):
        return []

    def uses_gaze(self):
        return False

    def get_prompt(self):
        return """Silent stationary rehearsal. Tools only, no speech.
On a request to run the sock demo, call mapped_sock_demo once. It maps three visible
floor socks and the tagged box, then rotates and picks/drops all three autonomously.
While running, wait. On completion or failure, wait; never automatically repeat.
No driving or navigation. Operator places all props within reach. Obey app Stop.
A new explicit request starts a fresh map; do not reuse a previous layout."""
