# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Auth headers per vendor. The Anthropic pair is a hard-won one: an organization-scoped
Console key is refused with a 400 ("not scoped to a workspace") until the request names the
workspace, while a workspace-scoped key must not carry the header."""

from __future__ import annotations

from innate_llm.configure import ANTHROPIC_WORKSPACE_ENV, Vendor, vendor_headers


def test_anthropic_sends_the_key_and_the_wire_version(monkeypatch) -> None:
    monkeypatch.delenv(ANTHROPIC_WORKSPACE_ENV, raising=False)
    headers = vendor_headers(Vendor.ANTHROPIC, "sk-ant-key")
    assert headers == {"x-api-key": "sk-ant-key", "anthropic-version": "2023-06-01"}


def test_an_organization_key_names_its_workspace(monkeypatch) -> None:
    monkeypatch.setenv(ANTHROPIC_WORKSPACE_ENV, " wrkspc_01ABC ")
    assert vendor_headers(Vendor.ANTHROPIC, "sk-ant-key")["anthropic-workspace-id"] == "wrkspc_01ABC"


def test_the_other_vendors_take_their_own_shape(monkeypatch) -> None:
    monkeypatch.setenv(ANTHROPIC_WORKSPACE_ENV, "wrkspc_01ABC")  # never leaves the Anthropic wire
    assert vendor_headers(Vendor.GOOGLE, "k") == {"x-goog-api-key": "k"}
    assert vendor_headers(Vendor.OPENAI, "k") == {"Authorization": "Bearer k"}
