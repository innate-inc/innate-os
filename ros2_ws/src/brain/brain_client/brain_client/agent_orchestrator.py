#!/usr/bin/env python3
"""
Agent orchestrator (meta-agent routing)

Builds a catalog from all loaded :class:`Agent` instances and selects which
agent id should handle a user task. Supports:

- **keyword**: fast overlap scoring over id, display name, skills, routing text
- **llm**: caller supplies a completion function (e.g. cloud OpenAI) that returns JSON

This module does not call the network by default; pass ``llm_complete`` when using LLM routing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from brain_client.agent_types import Agent

_LOG = logging.getLogger(__name__)

# Orchestration is often used from contexts that never call ``logging.basicConfig``; without a
# handler, INFO lines would be dropped by the root logger. Keep logs on stderr for this module only.
if not _LOG.handlers:
    _LOG.setLevel(logging.INFO)
    _stderr = logging.StreamHandler()
    _stderr.setLevel(logging.INFO)
    _stderr.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    _LOG.addHandler(_stderr)
    _LOG.propagate = False

# Loaded from ``agents/orchestrator_agent.py`` — default directive and routing fallback.
ORCHESTRATOR_AGENT_ID = "orchestrator_agent"

# Words ignored when scoring keyword overlap (keep small; domain terms stay meaningful)
_STOP_WORDS: frozenset = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "be",
        "it",
        "that",
        "this",
        "with",
        "as",
        "by",
        "from",
        "me",
        "my",
        "you",
        "your",
        "we",
        "our",
        "they",
        "their",
        "can",
        "could",
        "would",
        "should",
        "please",
        "just",
        "go",
        "get",
        "use",
        "using",
        "want",
        "need",
        "help",
        "make",
        "do",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
    }
)

_PROMPT_EXCERPT_LEN = 400

_LOG_PREVIEW_LEN = 200


def _preview(text: str, limit: int = _LOG_PREVIEW_LEN) -> str:
    """Single-line preview for logs (no newlines)."""
    if not text:
        return "(empty)"
    one = " ".join(text.strip().split())
    if len(one) <= limit:
        return one
    return one[: limit - 3] + "..."


_LLM_SYSTEM = """You are a routing assistant for a robot. Given the user task and optional context, choose exactly one agent id from the catalog. Reply with ONLY a JSON object, no markdown, in this form:
{"agent_id":"<id>","reason":"<one short sentence>"}
Rules:
- agent_id MUST be one of the listed ids.
- Prefer the default agent id from the catalog header for general conversation, unclear tasks, or when no specialist clearly applies.
- Only choose a non-default agent when the user clearly needs that agent's role or skills.
- reason must be under 200 characters."""


def _tokenize(text: str) -> Set[str]:
    if not text:
        return set()
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) > 1 and t not in _STOP_WORDS}


def _skill_basename(skill_id: str) -> str:
    if "/" in skill_id:
        return skill_id.rsplit("/", 1)[-1]
    return skill_id


@dataclass(frozen=True)
class AgentRouteDescriptor:
    """Snapshot of an agent for routing (no ROS / side effects)."""

    agent_id: str
    display_name: str
    skills: Tuple[str, ...]
    routing_text: str
    prompt_excerpt: str

    def search_blob(self) -> str:
        """Text used for keyword overlap."""
        parts = [
            self.agent_id.replace("_", " "),
            self.display_name,
            self.routing_text,
            " ".join(_skill_basename(s) for s in self.skills),
            self.prompt_excerpt,
        ]
        return " ".join(p for p in parts if p)


@dataclass
class OrchestrationResult:
    agent_id: str
    method: str
    confidence: float
    rationale: str


def list_agent_ids(agents: Mapping[str, Agent]) -> List[str]:
    """Stable ordering: sorted by agent id for deterministic prompts and tests."""
    return sorted(agents.keys())


def build_route_descriptors(
    agents: Mapping[str, Agent],
    *,
    exclude_ids: Optional[Iterable[str]] = None,
) -> List[AgentRouteDescriptor]:
    """
    Build routing descriptors for every loaded agent (e.g. from ``initialize_agents``).

    ``exclude_ids`` can omit meta-agents or internal directives from selection.
    """
    skip: Set[str] = set(exclude_ids or ())
    out: List[AgentRouteDescriptor] = []
    for aid in sorted(agents.keys()):
        if aid in skip:
            continue
        ag = agents[aid]
        prompt = ag.get_prompt()
        if prompt:
            excerpt = prompt.strip().replace("\n", " ")[:_PROMPT_EXCERPT_LEN]
        else:
            excerpt = ""
        custom = ag.get_routing_description()
        routing = (custom or "").strip()
        out.append(
            AgentRouteDescriptor(
                agent_id=ag.id,
                display_name=ag.display_name,
                skills=tuple(ag.get_skills()),
                routing_text=routing,
                prompt_excerpt=excerpt,
            )
        )
    return out


def format_catalog_markdown(
    descriptors: Sequence[AgentRouteDescriptor],
    *,
    default_agent_id: Optional[str] = None,
) -> str:
    """Human-readable catalog for LLM system/user prompts."""
    lines: List[str] = []
    if default_agent_id:
        lines.append(f"Default agent id (fallback): `{default_agent_id}`")
        lines.append("")
    for d in descriptors:
        skills = ", ".join(d.skills[:12])
        if len(d.skills) > 12:
            skills += ", …"
        lines.append(f"### {d.agent_id}")
        lines.append(f"- **Display name:** {d.display_name}")
        if d.routing_text:
            lines.append(f"- **Routing:** {d.routing_text}")
        elif d.prompt_excerpt:
            lines.append(f"- **Prompt excerpt:** {d.prompt_excerpt}")
        lines.append(f"- **Skills:** {skills}")
        lines.append("")
    return "\n".join(lines).strip()


def _keyword_scores(
    user_text: str,
    context: str,
    descriptors: Sequence[AgentRouteDescriptor],
) -> List[Tuple[str, float]]:
    user_tokens = _tokenize(f"{user_text} {context}")
    if not user_tokens:
        return [(d.agent_id, 0.0) for d in descriptors]

    scores: List[Tuple[str, float]] = []
    for d in descriptors:
        blob_tokens = _tokenize(d.search_blob())
        overlap = len(user_tokens & blob_tokens)
        # Light boost if agent id appears as substring in raw user message
        raw = f"{user_text} {context}".lower()
        bonus = 0.5 if d.agent_id.lower() in raw else 0.0
        scores.append((d.agent_id, float(overlap) + bonus))
    return scores


def select_agent_keyword(
    agents: Mapping[str, Agent],
    user_message: str,
    *,
    context: str = "",
    exclude_ids: Optional[Iterable[str]] = None,
    default_agent_id: Optional[str] = None,
    min_keyword_score_to_override_default: float = 2.0,
    min_margin_over_default: float = 1.0,
) -> OrchestrationResult:
    """
    Pick an agent using token overlap between user message + context and each agent profile.

    When ``default_agent_id`` is set and present in ``agents``, that agent is returned unless
    another agent both scores at least ``min_keyword_score_to_override_default`` and beats the
    default's score by more than ``min_margin_over_default``. This keeps the default agent for
    generic or ambiguous tasks.

    If ``default_agent_id`` is omitted, the highest-scoring agent wins (ties: lexicographic id).
    """
    _LOG.info(
        "##@@ [orchestrator:keyword] start user=%r context=%r default_agent_id=%r "
        "min_score_override=%s min_margin=%s exclude_ids=%s",
        _preview(user_message),
        _preview(context) if context else "(none)",
        default_agent_id,
        min_keyword_score_to_override_default,
        min_margin_over_default,
        sorted(exclude_ids) if exclude_ids else [],
    )

    descriptors = build_route_descriptors(agents, exclude_ids=exclude_ids)
    if not descriptors:
        raise ValueError("No agents available for orchestration")

    _LOG.info(
        "##@@ [orchestrator:keyword] catalog: %d agent(s) in routing set: %s",
        len(descriptors),
        [d.agent_id for d in descriptors],
    )

    scores = _keyword_scores(user_message, context, descriptors)
    scores_map = {aid: sc for aid, sc in scores}
    ranked = sorted(scores, key=lambda x: (-x[1], x[0]))
    for aid, sc in ranked:
        _LOG.info("##@@ [orchestrator:keyword] score %-40s %s", aid, f"{sc:.2f}")
    best_id, best_score = max(scores, key=lambda x: x[1])

    default_in_catalog = any(d.agent_id == default_agent_id for d in descriptors)
    use_default = bool(
        default_agent_id and default_agent_id in agents and default_in_catalog
    )

    if not use_default:
        _LOG.info(
            "##@@ [orchestrator:keyword] no default routing: use_default=False "
            "(default_agent_id=%r in_agents=%s in_catalog=%s)",
            default_agent_id,
            default_agent_id in agents if default_agent_id else False,
            default_in_catalog,
        )
        if best_score <= 0.0 and default_agent_id and default_agent_id in agents:
            _LOG.info(
                "##@@ [orchestrator:keyword] result: keep %r (no overlap, best was %r score=%s)",
                default_agent_id,
                best_id,
                best_score,
            )
            return OrchestrationResult(
                agent_id=default_agent_id,
                method="keyword",
                confidence=0.0,
                rationale="No keyword overlap; using default agent.",
            )

        max_possible = max((len(_tokenize(d.search_blob())) for d in descriptors), default=1)
        confidence = min(1.0, best_score / max(float(max_possible), 1.0))
        _LOG.info(
            "##@@ [orchestrator:keyword] result: selected %r score=%s confidence=%.3f (no default mode)",
            best_id,
            best_score,
            confidence,
        )
        return OrchestrationResult(
            agent_id=best_id,
            method="keyword",
            confidence=confidence,
            rationale="Highest overlap between user message and agent profile tokens.",
        )

    default_id = default_agent_id
    assert default_id is not None
    default_score = scores_map.get(default_id, 0.0)

    if best_id == default_id:
        max_possible = max((len(_tokenize(d.search_blob())) for d in descriptors), default=1)
        confidence = min(1.0, best_score / max(float(max_possible), 1.0))
        _LOG.info(
            "##@@ [orchestrator:keyword] result: default %r wins best_score=%s confidence=%.3f",
            default_id,
            best_score,
            max(confidence, 0.5),
        )
        return OrchestrationResult(
            agent_id=default_id,
            method="keyword",
            confidence=max(confidence, 0.5),
            rationale="Default agent matches best routing score.",
        )

    # Another agent scored higher than default; only switch if clearly ahead
    margin = best_score - default_score
    _LOG.info(
        "##@@ [orchestrator:keyword] override check: best=%r score=%s | default=%r score=%s | "
        "margin=%s | need score>=%s AND margin>%s",
        best_id,
        best_score,
        default_id,
        default_score,
        margin,
        min_keyword_score_to_override_default,
        min_margin_over_default,
    )
    if best_score < min_keyword_score_to_override_default or margin <= min_margin_over_default:
        max_possible = max((len(_tokenize(d.search_blob())) for d in descriptors), default=1)
        _LOG.info(
            "##@@ [orchestrator:keyword] result: STAY on default %r "
            "(thresholds block switch to %r: score_ok=%s margin_ok=%s)",
            default_id,
            best_id,
            best_score >= min_keyword_score_to_override_default,
            margin > min_margin_over_default,
        )
        return OrchestrationResult(
            agent_id=default_id,
            method="keyword",
            confidence=min(1.0, default_score / max(float(max_possible), 1.0)),
            rationale=(
                "Kept default agent: override thresholds not met "
                f"(need score ≥ {min_keyword_score_to_override_default} "
                f"and margin > {min_margin_over_default} over default)."
            ),
        )

    max_possible = max((len(_tokenize(d.search_blob())) for d in descriptors), default=1)
    confidence = min(1.0, best_score / max(float(max_possible), 1.0))
    _LOG.info(
        "##@@ [orchestrator:keyword] result: SWITCH default %r -> %r score=%s confidence=%.3f",
        default_id,
        best_id,
        best_score,
        confidence,
    )
    return OrchestrationResult(
        agent_id=best_id,
        method="keyword",
        confidence=confidence,
        rationale="Higher-confidence match than default agent; switching.",
    )


def _parse_llm_json(raw: str) -> Tuple[str, str]:
    raw = raw.strip()
    # Strip ```json fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    aid = data.get("agent_id")
    if not isinstance(aid, str) or not aid.strip():
        raise ValueError("Missing agent_id in LLM JSON")
    reason = data.get("reason", "")
    rationale = reason if isinstance(reason, str) else ""
    return aid.strip(), rationale


def select_agent_llm(
    agents: Mapping[str, Agent],
    user_message: str,
    *,
    context: str = "",
    llm_complete: Callable[[str, str], str],
    exclude_ids: Optional[Iterable[str]] = None,
    default_agent_id: Optional[str] = None,
) -> OrchestrationResult:
    """
    Ask an LLM to choose ``agent_id``. ``llm_complete(system_prompt, user_prompt)`` must
    return the model text (ideally raw JSON as specified in ``_LLM_SYSTEM``).

    Validates that the chosen id exists in ``agents``; on mismatch, falls back to
    ``default_agent_id`` or the first available id.
    """
    _LOG.info(
        "##@@ [orchestrator:llm] start user=%r context=%r default_agent_id=%r exclude_ids=%s",
        _preview(user_message),
        _preview(context) if context else "(none)",
        default_agent_id,
        sorted(exclude_ids) if exclude_ids else [],
    )

    descriptors = build_route_descriptors(agents, exclude_ids=exclude_ids)
    if not descriptors:
        raise ValueError("No agents available for orchestration")

    catalog = format_catalog_markdown(descriptors, default_agent_id=default_agent_id)
    user_prompt = f"USER TASK:\n{user_message}\n\nCONTEXT:\n{context or '(none)'}\n\nCATALOG:\n{catalog}"

    _LOG.debug(
        "##@@ [orchestrator:llm] prompt sizes: system=%d chars user=%d chars catalog_agents=%d",
        len(_LLM_SYSTEM),
        len(user_prompt),
        len(descriptors),
    )

    raw = llm_complete(_LLM_SYSTEM, user_prompt)
    _LOG.info(
        "##@@ [orchestrator:llm] raw model output (%d chars): %r",
        len(raw),
        _preview(raw, limit=500),
    )
    try:
        agent_id, rationale = _parse_llm_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        fb = default_agent_id or descriptors[0].agent_id
        chosen = fb if fb in agents else descriptors[0].agent_id
        _LOG.warning(
            "##@@ [orchestrator:llm] parse failed (%s); fallback to %r",
            e,
            chosen,
        )
        return OrchestrationResult(
            agent_id=chosen,
            method="llm",
            confidence=0.0,
            rationale=f"LLM output parse failed ({e}); fallback.",
        )

    if agent_id not in agents:
        fb = default_agent_id or descriptors[0].agent_id
        chosen = fb if fb in agents else descriptors[0].agent_id
        _LOG.warning(
            "##@@ [orchestrator:llm] invalid agent_id %r; fallback to %r",
            agent_id,
            chosen,
        )
        return OrchestrationResult(
            agent_id=chosen,
            method="llm",
            confidence=0.3,
            rationale=f"Invalid agent_id from LLM ({agent_id!r}); fallback.",
        )

    _LOG.info(
        "##@@ [orchestrator:llm] result: selected %r rationale=%r",
        agent_id,
        _preview(rationale, limit=160),
    )
    return OrchestrationResult(
        agent_id=agent_id,
        method="llm",
        confidence=1.0,
        rationale=rationale or "Selected by LLM.",
    )


class AgentOrchestrator:
    """
    Meta-agent helper: holds a mapping of agent id -> instance and exposes routing methods.

    By default, ``default_agent_id`` is :data:`ORCHESTRATOR_AGENT_ID` when that agent is
    loaded. Pass ``exclude_ids`` to omit agents from the routing catalog (e.g. ``{
    ORCHESTRATOR_AGENT_ID }`` if specialists should be the only routing targets).

    Typical use after ``initialize_agents``::

        orch = AgentOrchestrator(directives)
        choice = orch.select_keyword("Let's play chess")
        # publish choice.agent_id to /brain/set_directive, etc.
    """

    def __init__(
        self,
        agents: Mapping[str, Agent],
        *,
        default_agent_id: Optional[str] = None,
        exclude_ids: Optional[Iterable[str]] = None,
        min_keyword_score_to_override_default: float = 2.0,
        min_margin_over_default: float = 1.0,
    ) -> None:
        self._agents = dict(agents)
        if default_agent_id is None and ORCHESTRATOR_AGENT_ID in self._agents:
            self._default_agent_id: Optional[str] = ORCHESTRATOR_AGENT_ID
        else:
            self._default_agent_id = default_agent_id
        self._exclude_ids: Set[str] = set(exclude_ids or ())
        self._min_keyword_score_to_override_default = min_keyword_score_to_override_default
        self._min_margin_over_default = min_margin_over_default

    @property
    def agents(self) -> Mapping[str, Agent]:
        return self._agents

    def descriptors(self) -> List[AgentRouteDescriptor]:
        return build_route_descriptors(self._agents, exclude_ids=self._exclude_ids)

    def catalog_markdown(self) -> str:
        return format_catalog_markdown(
            self.descriptors(),
            default_agent_id=self._default_agent_id,
        )

    def select_keyword(self, user_message: str, *, context: str = "") -> OrchestrationResult:
        _LOG.info("##@@ [orchestrator] select_keyword() called")
        return select_agent_keyword(
            self._agents,
            user_message,
            context=context,
            exclude_ids=self._exclude_ids,
            default_agent_id=self._default_agent_id,
            min_keyword_score_to_override_default=self._min_keyword_score_to_override_default,
            min_margin_over_default=self._min_margin_over_default,
        )

    def select_llm(
        self,
        user_message: str,
        *,
        context: str = "",
        llm_complete: Callable[[str, str], str],
    ) -> OrchestrationResult:
        _LOG.info("##@@ [orchestrator] select_llm() called")
        return select_agent_llm(
            self._agents,
            user_message,
            context=context,
            llm_complete=llm_complete,
            exclude_ids=self._exclude_ids,
            default_agent_id=self._default_agent_id,
        )
