# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Where a dataset lives, on disk and on the Hugging Face Hub, and what a refused upload means."""

from __future__ import annotations

from pathlib import Path

from huggingface_hub.errors import HfHubHTTPError
from lerobot.utils.constants import HF_LEROBOT_HOME


def dataset_root(repo_id: str, root: str | Path | None) -> Path:
    return Path(root).expanduser() if root else HF_LEROBOT_HOME / repo_id


def hub_url(repo_id: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}"


def hub_refusal(error: HfHubHTTPError, repo_id: str) -> str:
    status = error.response.status_code if error.response is not None else None
    owner = repo_id.split("/", 1)[0]
    if status == 401:
        return "Hugging Face rejected the token. Log in again with `hf auth login`, or save a valid token in the web app's Settings."
    if status == 403:
        return f"The token may not write to {owner}. It needs write permission, and membership if {owner} is an organization."
    return f"Hugging Face refused the upload ({status}): {error}"
