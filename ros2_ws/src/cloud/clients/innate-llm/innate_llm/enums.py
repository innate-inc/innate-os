# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""``enum.StrEnum`` backport — the runtime is Python 3.10, StrEnum landed in 3.11."""

from enum import Enum


class StrEnum(str, Enum):
    """An enum whose members are (and format as) their plain string values."""

    __str__ = str.__str__
    __format__ = str.__format__
