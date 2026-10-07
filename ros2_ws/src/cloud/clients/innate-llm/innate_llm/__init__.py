# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""One small provider library for the brain: four pure adapters, one Http mover, frozen values.

:func:`configure` turns a ``vendor:name`` setting and the keys at hand into an
:class:`Llm`; callers compose their own policy into a
frozen :class:`Request`, a :class:`Provider` runs the one I/O loop, and
adapters only translate. Tests plug a :class:`~innate_llm.replay.Replay`
in at the same seam.
"""

from innate_llm.configure import Backend, Llm, Vendor, configure
from innate_llm.errors import Kind
from innate_llm.provider import Pinned, Provider
from innate_llm.types import (
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
    "Kind",
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
