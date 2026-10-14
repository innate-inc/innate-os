# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Agent
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.wave import Wave
from innate_skills.pick_sock_pile_yoloe import PickSockPileYoloe
from innate_skills.turn_in_place import TurnInPlace


class QwenSocksDemo3(Agent):
    """Collect handfuls from a sock pile, with app instructions and wave support."""

    model = "openai-chat:Qwen3.8-Flash-Next"
    model_extra_body = '{"chat_template_kwargs":{"enable_thinking":false}}'
    minimal_system_prompt = True
    wait_for_skill_completion = True
    history_max_entries = 12
    history_max_image_turns = 0

    @property
    def id(self) -> str:
        return "qwen_socks_demo_3"

    @property
    def display_name(self) -> str:
        return "Qwen Socks Demo 3"

    def get_inputs(self):
        return []

    def get_skills(self):
        return [PickSockPileYoloe, DropInBoxAruco, Wave, TurnInPlace]

    def uses_gaze(self) -> bool:
        return False

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
Obey app instructions and stop. Wave when asked after any running skill finishes.
No narration."""
