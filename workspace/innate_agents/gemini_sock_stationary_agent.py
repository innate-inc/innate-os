# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.sock_rehearsed_agent import SockRehearsedAgent
from innate_skills.drop_in_box_stationary import DropInBoxStationary
from innate_skills.pick_sock_stationary import PickSockStationary
from innate_skills.turn_in_place import TurnInPlace
from innate_skills.wave import Wave


class GeminiSockStationaryAgent(SockRehearsedAgent):
    """Silent Gemini rehearsal: rotation and arm motion only, no microphone."""

    model = "google:gemini-3.6-flash"

    @property
    def id(self) -> str:
        return "gemini_sock_stationary_agent"

    @property
    def display_name(self) -> str:
        return "Gemini Stationary Sock Demo"

    def get_skills(self):
        return [TurnInPlace, PickSockStationary, DropInBoxStationary, Wave]

    def get_prompt(self) -> str:
        return """You are Mars in a rehearsed sock demo. Stay completely silent: tools only,
no speech, commentary, or confirmations. The operator places socks within arm reach
and the marker box at the taught distance. You can rotate in place and move the arm;
you cannot drive forward, backward, or navigate.
When collecting: pick one visible floor sock using pick_sock_stationary with its color
and distinguishing features. After successful pickup, immediately use
drop_in_box_stationary; it rotates to find the box and releases from above.
After the drop finishes successfully, turn right 90 degrees with
turn_in_place(angle_degrees=-90), then pick the next floor sock. Repeat.
Wait silently for each skill to finish. Never pick socks already in the box.
If no floor sock is visible, turn right 90 degrees and look again, up to one full
turn per search. If none is found, wait silently. If a pickup or drop fails, wait
for the operator to reposition the prop; do not repeat the failed action.
Wave when requested through the app. Microphone input is disabled. Follow app stop commands."""
