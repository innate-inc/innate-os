# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The pre-#542 import paths are load-bearing: custom skills and agents on
robots in the field import ``brain_client.skill_types`` and
``brain_client.agent_types``, so the shims must keep resolving and re-export
the *same* class objects — registration via ``__init_subclass__`` and every
isinstance check depend on identity, not just equal names.

The agent-side end-to-end regression (legacy path → roster) lives in
test_agent_loading.py; the skill-side one is here, through the same
discovery functions the catalog calls (``_load_code_skills``)."""

import importlib
import logging
import sys
import textwrap

import pytest

from brain_client import agent_types, skill_types
from brain_client.agents import types as agents_types
from brain_client.skills import types as skills_types
from brain_client.skills.workspace_import import import_workspace_packages, registered_workspace_skills

LOGGER = logging.getLogger("test_compat_shims")


def test_agent_types_shim_reexports_same_objects():
    for name in ("Agent", "SkillRef", "InputRef"):
        assert getattr(agent_types, name) is getattr(agents_types, name)


def test_brain_transport_shim_reexports_same_objects():
    """workspace/inputs/micro_input.py imports pick_rest from the pre-#809 path,
    and a robot's customized copy of it keeps that line across an update — the
    shim is what stops a module move from taking the microphone down."""
    from brain_client.brain import transport as transport_shim
    from brain_client.brain.llm.gemini import transport as gemini_transport

    for name in ("pick_rest", "pick_transport", "GeminiRest", "GeminiHttpError", "GENERATE_PATH"):
        assert getattr(transport_shim, name) is getattr(gemini_transport, name)


def test_skill_types_shim_reexports_same_objects():
    for name in (
        "Skill",
        "SkillResult",
        "SkillOutput",
        "SkillCancelled",
        "RobotState",
        "RobotStateType",
        "Interface",
        "InterfaceType",
    ):
        assert getattr(skill_types, name) is getattr(skills_types, name)


# A skill exactly as robots in the field author them pre-#542: old import
# path, class-attribute Interface/RobotState declarations, tuple return.
LEGACY_SKILL_SRC = textwrap.dedent(
    '''
    from brain_client.skill_types import (
        Interface,
        InterfaceType,
        RobotState,
        RobotStateType,
        Skill,
        SkillResult,
    )


    class LegacyProbe(Skill):
        """Probe the head, legacy style."""

        head = Interface(InterfaceType.HEAD)
        odom = RobotState(RobotStateType.LAST_ODOM)

        def execute(self):
            return "ok", SkillResult.SUCCESS
    '''
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A synthetic $INNATE_OS_ROOT, with the process-global state discovery
    touches (sys.path, sys.modules, Skill._registry) snapshotted and restored
    — same isolation model as test_agent_loading's fixture."""
    (tmp_path / "workspace" / "custom_skills").mkdir(parents=True)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))

    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    saved_registry = dict(skills_types.Skill._registry)
    # Cleared, not just snapshotted: other test modules register Skills (some
    # deliberately broken), and discovery both rosters and *prunes* whatever
    # the live registry holds — the test must see only this workspace's.
    skills_types.Skill._registry.clear()
    yield tmp_path / "workspace"
    for name in set(sys.modules) - saved_modules:
        sys.modules.pop(name, None)
    sys.path[:] = saved_path
    skills_types.Skill._registry.clear()
    skills_types.Skill._registry.update(saved_registry)


def test_legacy_skill_loads_through_workspace_discovery(workspace):
    (workspace / "custom_skills" / "legacy_probe.py").write_text(LEGACY_SKILL_SRC)
    importlib.invalidate_caches()

    errors = import_workspace_packages(LOGGER)
    skills, broken = registered_workspace_skills(LOGGER)

    assert errors == {}
    assert broken == {}
    assert "local/legacy_probe" in skills
