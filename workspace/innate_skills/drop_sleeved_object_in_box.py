# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import SkillReturn
from innate_skills.drop_in_box_aruco import DropInBoxAruco
from innate_skills.pick_up_sleeved_object_astra import PickUpSleevedObjectAstra, visual_grip_held


class DropSleevedObjectInBox(DropInBoxAruco):
    """Drop the held sleeved object in the taught marker box, using visual grip checks."""

    _grip_llm = PickUpSleevedObjectAstra._grip_llm

    def _object_held(self):
        return visual_grip_held(self)

    def execute(self) -> SkillReturn:
        """Put the held sleeved keychain in the marker box."""
        super().execute()
        return 'Released the sleeved object over the box; landing was not visually verified.'
