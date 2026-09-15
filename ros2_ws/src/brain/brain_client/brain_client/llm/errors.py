# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The one error a caller sees."""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from brain_client.llm.types import Wire


class Kind(StrEnum):
    HTTP = "http"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    UNSUPPORTED = "unsupported"


_RETRYABLE_STATUSES = frozenset({408, 409, 429})


class LlmError(Exception):
    """The one error a caller sees; ``retryable`` is the library's whole opinion on what to do next."""

    def __init__(self, kind: Kind, detail: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(f"{kind}{f' {status}' if status is not None else ''}: {detail}")
        self.kind = kind
        self.status = status
        self.detail = detail
        self.retryable = retryable

    @classmethod
    def http(cls, status: int, detail: str) -> LlmError:
        return cls(Kind.HTTP, detail, status=status, retryable=status in _RETRYABLE_STATUSES or status >= 500)

    @classmethod
    def transport(cls, detail: str) -> LlmError:
        return cls(Kind.TRANSPORT, detail, retryable=True)

    @classmethod
    def protocol(cls, detail: str) -> LlmError:
        return cls(Kind.PROTOCOL, detail)

    @classmethod
    def unsupported(cls, what: str, wire: Wire) -> LlmError:
        return cls(Kind.UNSUPPORTED, f"{what} is not supported on the {wire} wire")
