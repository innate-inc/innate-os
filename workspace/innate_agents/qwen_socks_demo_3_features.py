# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Demo 3 using natural basket features for exclusion and drop localization."""

from innate_agents.qwen_socks_demo_3 import QwenSocksDemo3
from innate_skills.drop_in_box_features import DropInBoxFeatures
from innate_skills.pick_sock_pile_yoloe_features import PickSockPileYoloeFeatures
from innate_skills.turn_in_place import TurnInPlace
from innate_skills.wave import Wave


class QwenSocksDemo3Features(QwenSocksDemo3):
    @property
    def id(self) -> str:
        return "qwen_socks_demo_3_features"

    @property
    def display_name(self) -> str:
        return "Qwen Socks Demo 3 · Features"

    def get_skills(self):
        return [PickSockPileYoloeFeatures, DropInBoxFeatures, Wave, TurnInPlace]

    def get_prompt(self) -> str:
        return (super().get_prompt()
                .replace("pick_sock_pile_yoloe", "pick_sock_pile_yoloe_features")
                .replace("drop_in_box_aruco", "drop_in_box_features"))
