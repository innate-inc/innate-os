# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for import-based agent discovery (agents/loader.py + initializer.py).

The load path is pure Python (no ROS), so these run under plain pytest against
a synthetic ``$INNATE_OS_ROOT`` workspace built in ``tmp_path``. What they pin
is exactly what the import model exists to guarantee: **nothing silently
vanishes** — every authoring mistake (broken module, abstract class, probe
failure, function-local class, key collision) becomes a visible broken-roster
row with its real error, and ordinary Python (helpers, folder agents,
multi-agent files) just works.
"""

import importlib
import logging
import sys
import textwrap

import pytest

from brain_client.agents import initializer as agent_initializer
from brain_client.agents.initializer import initialize_agents
from brain_client.agents.loader import build_agent_instances, discover_agent_classes
from brain_client.agents.types import Agent
from brain_client.skills.workspace_import import unique_key

LOGGER = logging.getLogger("test_agent_loading")

# Minimal concrete agent. `body` lets a test inject a probe-time failure.
AGENT_SRC = """
from brain_client.agents.types import Agent


class {cls}(Agent):
    @property
    def id(self):
        return {id!r}

    @property
    def display_name(self):
        return {id!r}

    def get_skills(self):
        return []

    def get_prompt(self):
        return "test prompt"
{body}
"""


def agent_src(cls: str, agent_id: str | None = None, body: str = "") -> str:
    return AGENT_SRC.format(cls=cls, id=agent_id or cls.lower(), body=textwrap.indent(body, "    "))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A synthetic $INNATE_OS_ROOT with empty agent packages, with all the
    process-global state the loader touches (sys.path, sys.modules,
    Agent._registry, the initializer's ids-by-module carryover) snapshotted
    and restored so tests can't see each other's imports."""
    (tmp_path / "workspace" / "innate_agents").mkdir(parents=True)
    (tmp_path / "workspace" / "custom_agents").mkdir(parents=True)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))

    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    saved_registry = dict(Agent._registry)
    agent_initializer._agent_ids_by_module.clear()
    yield tmp_path / "workspace"
    for name in set(sys.modules) - saved_modules:
        sys.modules.pop(name, None)
    sys.path[:] = saved_path
    Agent._registry.clear()
    Agent._registry.update(saved_registry)
    agent_initializer._agent_ids_by_module.clear()


def write(workspace, relpath: str, content: str) -> None:
    path = workspace / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    # New files/dirs must be visible to the import system immediately, even
    # inside the directory-listing cache TTL.
    importlib.invalidate_caches()


# ---------------------------------------------------------------- happy path


def test_agents_load_with_source_stamping(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    write(workspace, "custom_agents/beta.py", agent_src("Beta"))

    agents, default_agent, broken = initialize_agents(LOGGER)

    assert broken == {}
    assert set(agents) == {"alpha", "beta"}
    assert agents["alpha"].source == "shipped"
    assert agents["beta"].source == "user"
    assert default_agent is agents["alpha"]  # no intro_agent -> first loaded


def test_intro_agent_is_default(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    write(workspace, "innate_agents/intro_agent.py", agent_src("IntroAgent", "intro_agent"))

    _agents, default_agent, _broken = initialize_agents(LOGGER)

    assert default_agent.id == "intro_agent"


def test_ordinary_python_works(workspace):
    # A helper module next to an agent, imported by bare name; a folder agent;
    # a multi-agent file. None of this worked under the file-exec loader.
    write(workspace, "innate_agents/prompts.py", 'GREETING = "hi"\n')
    write(
        workspace,
        "innate_agents/alpha.py",
        "from innate_agents.prompts import GREETING\n" + agent_src("Alpha"),
    )
    write(workspace, "innate_agents/folder_agent/__init__.py", agent_src("FolderAgent"))
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo"))

    agents, _default, broken = initialize_agents(LOGGER)

    assert broken == {}
    assert set(agents) == {"alpha", "folderagent", "pairone", "pairtwo"}


def test_legacy_and_facade_import_paths_load(workspace):
    # Robots in the field have custom agents authored against the pre-#542
    # `brain_client.agent_types` path; new ones author against `innate`.
    # Both must resolve to the same Agent class or field agents break on update.
    write(
        workspace,
        "custom_agents/legacy.py",
        agent_src("Legacy").replace("brain_client.agents.types", "brain_client.agent_types"),
    )
    write(
        workspace,
        "custom_agents/facade.py",
        agent_src("Facade").replace("from brain_client.agents.types import Agent", "from innate import Agent"),
    )

    agents, _default, broken = initialize_agents(LOGGER)

    assert broken == {}
    assert {"legacy", "facade"} <= set(agents)


# ------------------------------------------------------- broken stays visible


def test_broken_module_rosters_with_file_and_line(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    write(workspace, "innate_agents/bad.py", "raise RuntimeError('boom')\n")

    agents, _default, broken = initialize_agents(LOGGER)

    assert set(agents) == {"alpha"}  # the good agent is unaffected
    assert list(broken) == ["bad"]  # module row keyed like a class row
    assert "RuntimeError: boom" in broken["bad"]
    assert "innate_agents/bad.py:1" in broken["bad"]  # format_load_error points at the file


def test_abstract_agent_rosters_broken_helper_base_hidden(workspace):
    # A missing member (misspelled get_prompt) makes the class abstract: it
    # must roster broken naming the gap, not vanish. A deliberate `_` helper
    # base stays hidden.
    write(
        workspace,
        "innate_agents/half.py",
        textwrap.dedent(
            """
            from brain_client.agents.types import Agent


            class _HelperBase(Agent):
                pass


            class Half(Agent):
                @property
                def id(self):
                    return "half"

                @property
                def display_name(self):
                    return "half"

                def get_skills(self):
                    return []
            """
        ),
    )

    agents, _default, broken = initialize_agents(LOGGER)

    assert agents == {}
    assert list(broken) == ["half"]
    assert "get_prompt" in broken["half"]
    assert not any("helper" in key.lower() for key in broken)


def test_probe_failure_rosters_broken(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    write(
        workspace,
        "innate_agents/lazy.py",
        agent_src("Lazy", body='def get_inputs(self):\n    raise ValueError("no input device")'),
    )

    agents, _default, broken = initialize_agents(LOGGER)

    assert set(agents) == {"alpha"}
    assert list(broken) == ["lazy"]
    assert "ValueError: no input device" in broken["lazy"]


def test_function_local_agent_rosters_broken(workspace):
    write(
        workspace,
        "innate_agents/factory.py",
        textwrap.dedent(
            """
            from brain_client.agents.types import Agent


            def make():
                class Local(Agent):  # registers at call time, unreachable from the module
                    pass


            make()
            """
        ),
    )

    _agents, _default, broken = initialize_agents(LOGGER)

    # Keyed like module rows: package prefix stripped for display.
    assert list(broken) == ["factory.local"]
    assert "define it at module level" in broken["factory.local"]


# ----------------------------------------------------------- id precedence


def test_custom_wins_id_conflict_and_stays_stable(workspace):
    write(workspace, "innate_agents/shipped.py", agent_src("Shipped", "shared"))
    write(workspace, "custom_agents/override.py", agent_src("Override", "shared"))

    for load_pass in range(2):  # the original bug only inverted on the SECOND load
        agents, _default, broken = initialize_agents(LOGGER)
        assert broken == {}
        assert type(agents["shared"]).__name__ == "Override", f"pass {load_pass}"
        assert agents["shared"].source == "user"


# ------------------------------------------------------------ key collisions


def test_unique_key_counts_up():
    taken = {"a": 1, "a.2": 1}
    assert unique_key(taken, "a") == "a.3"
    assert unique_key(taken, "b") == "b"


def test_same_named_broken_modules_both_roster(workspace):
    write(workspace, "innate_agents/bad.py", "raise RuntimeError('innate boom')\n")
    write(workspace, "custom_agents/bad.py", "raise RuntimeError('custom boom')\n")

    _agents, _default, broken = initialize_agents(LOGGER)

    assert len(broken) == 2  # neither error shadowed the other
    assert {err.split("RuntimeError: ")[1] for err in broken.values()} == {"innate boom", "custom boom"}


def test_broken_class_never_shadows_broken_module(workspace):
    # A probe-failing class whose snake name equals a failed module's row key.
    write(workspace, "innate_agents/clash.py", "raise RuntimeError('module boom')\n")
    write(
        workspace,
        "custom_agents/other.py",
        agent_src("Clash", body='def get_inputs(self):\n    raise ValueError("class boom")'),
    )

    _agents, _default, broken = initialize_agents(LOGGER)

    assert len(broken) == 2
    errors = " | ".join(broken.values())
    assert "module boom" in errors
    assert "class boom" in errors


# ------------------------------------------------------------------- reload


def test_break_then_fix_reload_cycle(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"alpha"} and broken == {}

    write(workspace, "innate_agents/alpha.py", "raise RuntimeError('edited badly')\n")
    agents, _default, broken = initialize_agents(LOGGER)
    assert agents == {}
    assert "RuntimeError: edited badly" in broken["alpha"]

    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"alpha"} and broken == {}


def test_reload_returns_fresh_instances(workspace):
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))
    first, _default, _broken = initialize_agents(LOGGER)
    second, _default, _broken = initialize_agents(LOGGER)

    assert first["alpha"] is not second["alpha"]
    assert type(first["alpha"]) is not type(second["alpha"])  # re-imported class


def test_broken_multi_agent_module_rosters_every_agent(workspace):
    # A module that defined N agents on its last clean import must roster N
    # broken rows when it breaks — not collapse to one module row, silently
    # vanishing every agent but one.
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo"))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"pairone", "pairtwo"} and broken == {}

    write(workspace, "innate_agents/pair.py", "raise RuntimeError('pair boom')\n")
    agents, _default, broken = initialize_agents(LOGGER)
    assert agents == {}
    assert set(broken) == {"pairone", "pairtwo"}  # both ids, carried from the clean import
    assert all("RuntimeError: pair boom" in err for err in broken.values())

    # The knowledge survives consecutive broken loads...
    _agents, _default, broken = initialize_agents(LOGGER)
    assert set(broken) == {"pairone", "pairtwo"}

    # ...and the rows heal on fix.
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo"))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"pairone", "pairtwo"} and broken == {}


def test_probe_failed_module_keeps_identity_through_import_failure(workspace):
    # healthy -> every agent fails probing -> module fails to import. The
    # probe-fail round builds no instances, so rebuilding the ids-by-module
    # carryover from live agents alone would erase the module's identity and
    # the final round would collapse to one module-derived row.
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo"))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"pairone", "pairtwo"} and broken == {}

    probe_fail = 'def get_inputs(self):\n    raise ValueError("probe boom")'
    write(
        workspace,
        "innate_agents/pair.py",
        agent_src("PairOne", body=probe_fail) + agent_src("PairTwo", body=probe_fail),
    )
    agents, _default, broken = initialize_agents(LOGGER)
    assert agents == {}
    assert set(broken) == {"pair_one", "pair_two"}  # probe rows key by class name

    write(workspace, "innate_agents/pair.py", "raise RuntimeError('pair boom')\n")
    agents, _default, broken = initialize_agents(LOGGER)
    assert agents == {}
    assert set(broken) == {"pairone", "pairtwo"}  # ids survived the probe-fail round
    assert all("RuntimeError: pair boom" in err for err in broken.values())


def test_partially_probe_failed_module_keeps_both_ids(workspace):
    # Same, but only one of the two agents fails probing: the carryover must
    # union the still-live id with the remembered one, not keep just the
    # survivor.
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo"))
    initialize_agents(LOGGER)

    probe_fail = 'def get_inputs(self):\n    raise ValueError("probe boom")'
    write(workspace, "innate_agents/pair.py", agent_src("PairOne") + agent_src("PairTwo", body=probe_fail))
    agents, _default, broken = initialize_agents(LOGGER)
    assert set(agents) == {"pairone"}
    assert set(broken) == {"pair_two"}

    write(workspace, "innate_agents/pair.py", "raise RuntimeError('pair boom')\n")
    _agents, _default, broken = initialize_agents(LOGGER)
    assert set(broken) == {"pairone", "pairtwo"}


# ------------------------------------------------------------ loader pieces


def test_discover_orders_innate_before_custom(workspace):
    write(workspace, "custom_agents/beta.py", agent_src("Beta"))
    write(workspace, "innate_agents/alpha.py", agent_src("Alpha"))

    classes, errors = discover_agent_classes(LOGGER)

    assert errors == {}
    assert [cls.__module__.partition(".")[0] for cls, _src in classes] == ["innate_agents", "custom_agents"]


def test_build_reports_id_conflict_last_wins(workspace):
    write(workspace, "innate_agents/one.py", agent_src("One", "same_id"))
    write(workspace, "innate_agents/two.py", agent_src("Two", "same_id"))

    classes, _errors = discover_agent_classes(LOGGER)
    agents, broken = build_agent_instances(classes, LOGGER)

    assert broken == {}
    assert list(agents) == ["same_id"]  # conflict warns, latter wins (documented behavior)
