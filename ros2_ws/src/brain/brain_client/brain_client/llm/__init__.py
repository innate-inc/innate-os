# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One small provider library for the brain: four pure adapters, one Http mover, frozen values.

:func:`configure` turns a ``vendor:name`` setting and the keys at hand into an
:class:`Llm`; callers compose policy (:mod:`~brain_client.brain.history`) into a
frozen :class:`Request`, a :class:`Provider` runs the one I/O loop, and
adapters only translate. Tests plug a :class:`~brain_client.llm.replay.Replay`
in at the same seam.
"""

from brain_client.llm.configure import Backend, Llm, Vendor, configure
from brain_client.llm.provider import Pinned, Provider
from brain_client.llm.types import (
    Audio,
    Capabilities,
    Event,
    Finish,
    Image,
    Json,
    LlmError,
    Message,
    Model,
    Part,
    Reply,
    Request,
    Role,
    Text,
    TextDelta,
    Thinking,
    Thought,
    ThoughtDelta,
    Tool,
    ToolCall,
    ToolResult,
    Usage,
    Wire,
)

__all__ = [
    "Audio",
    "Backend",
    "Capabilities",
    "Event",
    "Finish",
    "Image",
    "Json",
    "Llm",
    "LlmError",
    "Message",
    "Model",
    "Part",
    "Pinned",
    "Provider",
    "Reply",
    "Request",
    "Role",
    "Text",
    "TextDelta",
    "Thinking",
    "Thought",
    "ThoughtDelta",
    "Tool",
    "ToolCall",
    "ToolResult",
    "Usage",
    "Vendor",
    "Wire",
    "configure",
]
