# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deprecated: moved to brain_client.brain.llm.gemini.transport.

Backward-compat shim for custom input devices that still import from the old
path. ``workspace/`` lives on the robot and is user-editable, so a customized
micro_input.py keeps its own import line across an update — moving the module
out from under it would take the microphone down. Remove in a future release
once external input files have migrated.
"""

from brain_client.brain.llm.gemini.transport import *  # noqa: F401, F403
