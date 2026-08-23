# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Backend-independent speech playback events."""

from collections.abc import Callable

from brain_client.common.enums import StrEnum


class PlaybackEvent(StrEnum):
    STARTED = "started"
    NEAR_END = "near_end"
    ENDED = "ended"
    ABORTED = "aborted"


PlaybackObserver = Callable[[PlaybackEvent], None]
