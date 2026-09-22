# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Where a dataset lives, on disk and on the Hugging Face Hub, and what a refused upload means."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from huggingface_hub import HfApi
from huggingface_hub.errors import HfHubHTTPError
from lerobot.utils.constants import HF_LEROBOT_HOME

if TYPE_CHECKING:
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

DATASET_DIRS = ("data", "meta", "videos")


def dataset_root(repo_id: str, root: str | Path | None) -> Path:
    return Path(root).expanduser() if root else HF_LEROBOT_HOME / repo_id


def has_dataset(root: Path) -> bool:
    return (root / "meta" / "info.json").is_file()


def hub_url(repo_id: str) -> str:
    return f"https://huggingface.co/datasets/{repo_id}"


def push_mirror(dataset: LeRobotDataset, *, private: bool) -> None:
    """Upload `dataset` so that the Hub holds exactly this copy, no more.

    push_to_hub uploads but never deletes, and lerobot loads every parquet file under data/ and
    meta/episodes/, so a copy rebuilt with fewer files would leave the Hub serving the old episodes as
    well. One commit therefore uploads this copy and deletes every other file in the dataset folders:
    if it fails, the Hub keeps the previous dataset whole. push_to_hub then finds nothing left to
    upload and only refreshes the dataset card and moves lerobot's version tag.
    """
    api = HfApi()
    if api.repo_exists(dataset.repo_id, repo_type="dataset"):
        api.upload_folder(
            repo_id=dataset.repo_id,
            repo_type="dataset",
            folder_path=dataset.root,
            ignore_patterns=["images/"],  # lerobot's own push leaves the raw frame scratch folder out too
            delete_patterns=[f"{folder}/" for folder in DATASET_DIRS],
            commit_message="Replace the dataset with the current copy",
        )
    dataset.push_to_hub(private=private)


def hub_refusal(error: HfHubHTTPError, repo_id: str) -> str:
    status = error.response.status_code if error.response is not None else None
    owner = repo_id.split("/", 1)[0]
    if status == 401:
        return "Hugging Face rejected the token. Log in again with `hf auth login`, or save a valid token in the web app's Settings."
    if status == 403:
        return f"The token may not write to {owner}. It needs write permission, and membership if {owner} is an organization."
    return f"Hugging Face refused the upload ({status}): {error}"
