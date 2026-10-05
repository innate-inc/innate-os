# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_agents.qwen_sock_demo_2 import QwenSockDemo2
from innate_skills.pick_sock_pile_yoloe import PickSockPileYoloe
from innate_skills.pick_sock_yoloe import PickSockYoloe
from innate_skills.turn_in_place import TurnInPlace


class QwenSocksDemo3(QwenSockDemo2):
    """Collect handfuls from a sock pile, with microphone and wave support."""

    @property
    def id(self) -> str:
        return "qwen_socks_demo_3"

    @property
    def display_name(self) -> str:
        return "Qwen Socks Demo 3"

    def get_skills(self):
        return [PickSockPileYoloe if skill is PickSockYoloe else skill for skill in super().get_skills()] + [TurnInPlace]

    def get_prompt(self) -> str:
        return """Collect as many socks as possible per grasp from a floor pile; tools only.
Call pick_sock_pile_yoloe. It finds a pile and grasps its center to try to catch several socks.
Pickup success -> drop_in_box_aruco. Drop success -> pick_sock_pile_yoloe. Repeat.
The pile is left of the box; pickup searches left.
Pick only socks lying on the floor away from the box. Never pick socks inside the box,
on its rim or sides, or draped over its edges. If only those socks are visible,
use turn_in_place to look for floor socks.
Use turn_in_place when asked to rotate or to look around. Pickup/drop handle their own approach.
Wait while a skill runs. Trust completion events; sock count is not verified.
On failure, wait for instructions; never drop after failed pickup.
Obey spoken/app instructions and stop. Wave when asked after any running skill finishes.
No narration."""
