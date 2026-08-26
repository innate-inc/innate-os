# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Generic known-map visual coverage built on the proven search planner."""

from innate_skills.find_next_person import FindNextPerson

from innate import SkillOutput, SkillReturn

_STATUS_NAMES = {
    "SEARCH_RESET": "EXPLORATION_RESET",
    "SEARCH_ALREADY_INITIALIZED": "EXPLORATION_ALREADY_INITIALIZED",
    "SEARCH_OBSERVATION": "EXPLORATION_OBSERVATION",
    "SEARCH_UNREACHABLE": "EXPLORATION_UNREACHABLE",
    "SEARCH_INFRASTRUCTURE_FAILURE": "EXPLORATION_INFRASTRUCTURE_FAILURE",
    "SEARCH_VISUALIZATION": "EXPLORATION_VISUALIZATION",
    "SEARCH_EXHAUSTED": "EXPLORATION_COMPLETE",
}


class ExploreMap(FindNextPerson):
    """Move through safe known-free viewpoints until camera coverage is complete.

    Unknown, occupied, unreachable, and keepout cells are excluded by the
    underlying map planner. Each successful call moves to one nearby useful
    viewpoint, deliberately turns toward uncovered floor, and returns a fresh
    camera image. ``reset`` starts new coverage; ``visualize`` only renders it.
    """

    def execute(self, reset: bool = False, visualize: bool = False) -> SkillReturn:
        output = super().execute(reset=reset, visualize=visualize)
        if not isinstance(output, SkillOutput):
            return output
        code, separator, remainder = output.message.partition(" ")
        translated = _STATUS_NAMES.get(code, code)
        return SkillOutput(
            f"{translated}{separator}{remainder}",
            data=output.data,
            status=output.status,
            image=output.image,
        )
