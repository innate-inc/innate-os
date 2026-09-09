# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the scribe asks Gemini, once per window (RFC 6.3): the system prompt
that says what may be written down at all, the schema the answer has to come
back in, and the window rendered as the text the model reads — the transcript,
who was in view for each line, and the facts already on file so it can supersede
rather than duplicate. PURE module: no transport, no I/O."""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.people.memory import FACT_TEXT_LIMIT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from brain_client.people.memory import Fact
    from brain_client.people.transcript import Window

_MAX_CONTEXT_FACTS = 20  # the existing facts the scribe is shown per person, so it can supersede

SYSTEM_TEXT = (
    "You are the scribe of a small home robot. You read a short window of the conversation the "
    "robot just had, together with who was visible (tagged P1, P2 …) for each line and what the "
    "robot already knows about them, and you write down only what is worth remembering about a "
    "person.\n"
    "Rules:\n"
    "- Report only what was actually said. Never infer emotion, mood, ethnicity, gender, age, "
    "health or any other attribute of a person from how they look or sound; if it was not said, "
    "it does not exist.\n"
    "- Attribute every item to the tag of the person it is about, and quote the exact words it "
    "came from together with that line's utterance id. If you cannot tell which visible person a "
    'statement is about, still give your best tag but set attribution to "uncertain".\n'
    '- attribution: "self" when the person said it about themselves, "third_party" when '
    'somebody else said it about them, "owner" when the robot\'s owner did, "robot" when the '
    "robot itself concluded it out loud.\n"
    '- When a statement updates something already on file, set "supersedes" to that fact\'s id '
    "instead of writing a near-duplicate.\n"
    '- name_candidates: report any name heard for a visible person. Set "introduction" to '
    '"self" for a first-person introduction ("I\'m Ana"), "owner" when the owner names them, '
    '"third_party" when somebody introduces them ("this is my daughter Zoe"), "other" otherwise. '
    'Set "referent" to the tag the transcript itself identifies, or leave it empty when the words '
    'do not say which visible person is meant. Set "correction" when the words correct a name '
    "already on file. Never guess which person a name belongs to; the robot resolves that itself.\n"
    "- sensitive: health, finances, relationships in trouble, anything a person would not want "
    "repeated. Put those there rather than in facts, whether or not they asked the robot to "
    "remember them.\n"
    '- open_loops: things promised, requested or due ("find the blue socks by tomorrow").\n'
    "- episode_note: at most two sentences on what happened in this window, or an empty string.\n"
    "- Return empty lists when the window holds nothing worth keeping. That is the normal case."
)

_FACT_ITEM = {
    "type": "OBJECT",
    "properties": {
        "who": {"type": "STRING", "description": "the tag of the person the item is about, e.g. P2"},
        "kind": {
            "type": "STRING",
            "enum": ["identity", "preference", "biography", "relationship", "routine", "request"],
        },
        "text": {"type": "STRING", "description": f"at most {FACT_TEXT_LIMIT} characters"},
        "confidence": {"type": "NUMBER"},
        "attribution": {"type": "STRING", "enum": ["self", "third_party", "robot", "owner", "uncertain"]},
        "quote": {"type": "STRING", "description": "the exact words this came from"},
        "utterance": {"type": "STRING", "description": "the id of the line quoted"},
        "supersedes": {"type": "STRING", "description": "id of the fact this replaces, else empty"},
    },
    "required": ["who", "kind", "text", "confidence", "attribution", "quote", "utterance", "supersedes"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "facts": {"type": "ARRAY", "items": _FACT_ITEM},
        "name_candidates": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "who": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "quote": {"type": "STRING"},
                    "utterance": {"type": "STRING"},
                    "introduction": {"type": "STRING", "enum": ["self", "owner", "third_party", "other"]},
                    "referent": {"type": "STRING", "description": "tag the transcript names, else empty"},
                    "correction": {"type": "BOOLEAN"},
                },
                "required": [
                    "who",
                    "name",
                    "confidence",
                    "quote",
                    "utterance",
                    "introduction",
                    "referent",
                    "correction",
                ],
            },
        },
        "open_loops": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "who": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "due": {"type": "STRING", "description": "YYYY-MM-DD or empty"},
                    "quote": {"type": "STRING", "description": "the exact words the promise was made in"},
                    "utterance": {"type": "STRING", "description": "the id of the line quoted"},
                },
                "required": ["who", "text", "due", "quote", "utterance"],
            },
        },
        "episode_note": {"type": "STRING"},
        "appearance_notes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"who": {"type": "STRING"}, "text": {"type": "STRING"}},
                "required": ["who", "text"],
            },
        },
        "sensitive": {"type": "ARRAY", "items": _FACT_ITEM},
    },
    "required": ["facts", "name_candidates", "open_loops", "episode_note", "appearance_notes", "sensitive"],
}

_GENERATION_CONFIG = {
    "responseMimeType": "application/json",
    "responseSchema": RESPONSE_SCHEMA,
    "thinkingConfig": {"thinkingLevel": "low"},
}


def build_request(window: Window, known_facts: Mapping[str, Sequence[Fact]] | None = None) -> dict:
    """The one Gemini call per window: the transcript, who was in view for each
    line, and the facts already on file for them (so the model can supersede
    rather than duplicate). ``known_facts`` is keyed by tag."""
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_TEXT}]},
        "contents": [{"role": "user", "parts": [{"text": window_text(window, known_facts or {})}]}],
        "generationConfig": _GENERATION_CONFIG,
    }


def window_text(window: Window, known_facts: Mapping[str, Sequence[Fact]]) -> str:
    """The window as the model reads it."""
    lines = ["People visible during this window:"]
    views = window.views()
    if not views:
        lines.append("- nobody was in view")
    for tag, view in sorted(views.items()):
        name = view.name or "unknown"
        lines.append(f"- {tag} = {name} ({view.state}{', enrolling' if view.enrolling else ''})")
        for fact in list(known_facts.get(tag, ()))[:_MAX_CONTEXT_FACTS]:
            lines.append(f"    on file [{fact.id}] {fact.kind}: {fact.text}")
    lines.append("")
    lines.append("Transcript:")
    for message in window.messages:
        in_view = ", ".join(seen.tag for seen in message.in_view) or "nobody"
        lines.append(f'[{message.id}] {message.speaker} (in view: {in_view}): "{message.text}"')
    lines.append("")
    lines.append("What is worth remembering about the people in view?")
    return "\n".join(lines)
