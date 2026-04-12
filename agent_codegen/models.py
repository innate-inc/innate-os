"""
Data models and exceptions for agent_codegen.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class AgentCodegenError(RuntimeError):
    """Raised for all library-level failures.

    Covers: empty or invalid input, API errors, code validation failures.
    """


@dataclass
class GenerationResult:
    """Result returned by :func:`generate_agent`.

    Attributes:
        agent_id:   Snake-case identifier taken from the missing-capabilities spec key.
        code:       Complete Python source of the generated agent as a string.
        file_path:  Absolute path of the written file, or ``None`` when no
                    ``output_path`` was supplied.
        via_tool:   ``True`` when the model called the ``write_agent_file`` tool
                    (structured output); ``False`` when code was extracted from a
                    markdown code block as a fallback.
    """

    agent_id: str
    code: str
    file_path: Optional[str]
    via_tool: bool
