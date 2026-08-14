# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Fixtures shared by both halves of the provisioning tests — the BLE command layer and
the root applier. VALID_ENV encodes the validation contract both of them enforce, so it
lives once or the two copies drift apart silently."""

SERVICE_KEY = "innate-test-key-not-a-real-credential"
VALID_ENV = f"INNATE_SERVICE_KEY={SERVICE_KEY}\nROBOT_ID=R7-41\nROBOT_NAME=MARS the 41st\nCOLOR_VARIANT=blue\n"
