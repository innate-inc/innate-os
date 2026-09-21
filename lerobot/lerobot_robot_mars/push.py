# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""mars-push: upload a dataset recorded with lerobot-record to the Hugging Face Hub.

lerobot has no command for a dataset that was recorded without --dataset.push_to_hub; this wraps
the call its own tools use, which also writes the dataset card and the version tag a plain file
upload would leave out.
"""

from __future__ import annotations

import argparse
import sys

from huggingface_hub import HfApi, get_token
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from .hub import dataset_root, has_dataset, hub_refusal, hub_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mars-push", description="Upload a recorded LeRobot dataset to the Hub.")
    parser.add_argument("repo_id", help="account/dataset, the --dataset.repo_id it was recorded under")
    parser.add_argument("--root", help="the dataset's folder; default $HF_LEROBOT_HOME/<repo_id>")
    parser.add_argument("--private", action="store_true", help="create the Hub repository as private")
    args = parser.parse_args(argv)

    root = dataset_root(args.repo_id, args.root)
    if not has_dataset(root):
        parser.error(f"no dataset at {root}")
    if get_token() is None:
        parser.error("not logged in to Hugging Face; run `uv run hf auth login` first")
    try:
        if args.private and _is_public(args.repo_id):
            print(
                f"{args.repo_id} is already public and stays so: --private only applies when the repository is created."
            )
            print(f"Change it at {hub_url(args.repo_id)}/settings")
        LeRobotDataset(args.repo_id, root=root).push_to_hub(private=args.private)
    except HfHubHTTPError as e:
        print(hub_refusal(e, args.repo_id), file=sys.stderr)
        return 1
    print(f"Published {hub_url(args.repo_id)}")
    return 0


def _is_public(repo_id: str) -> bool:
    try:
        return not HfApi().repo_info(repo_id, repo_type="dataset").private
    except RepositoryNotFoundError:
        return False


if __name__ == "__main__":
    sys.exit(main())
