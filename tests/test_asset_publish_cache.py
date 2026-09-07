# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the publish script without downloading assets or pushing images."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/test/innate-os-sim-assets"


@pytest.fixture
def publish(tmp_path):
    trace = tmp_path / "calls.jsonl"
    # Only the external registry/build operations are replaced. Bash executes
    # the real script, including input validation, cache keys and the probe.
    stub = (
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
with open(os.environ['PUBLISH_TRACE'], 'a') as log:
    log.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
if '--exists' in sys.argv:
    sys.exit(0 if os.environ.get('PROBE_EXISTS') == 'true' else 1)
"""
    )
    for name in ("python3", "docker"):
        executable = tmp_path / name
        executable.write_text(stub)
        executable.chmod(0o755)

    def run(**overrides):
        trace.unlink(missing_ok=True)
        env = {key: value for key, value in os.environ.items() if key not in {"CACHE_SCOPE", "PROBE_EXISTS"}}
        env.update(
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            PUBLISH_TRACE=str(trace),
            IMAGE_PREFIX="ghcr.io/test",
            IMAGE_TAG="sha-test",
            IMAGE_INPUTS_HASH="test",
            PUSH_MAIN_TAGS="false",
        )
        env.update(overrides)
        result = subprocess.run(["bash", "ci/build_assets_image.sh"], cwd=ROOT, env=env, capture_output=True, text=True)
        calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        return result, calls

    return run


def values(command, flag):
    return [command[i + 1] for i, arg in enumerate(command) if arg == flag]


def test_main_cache_and_existing_image_short_circuit(publish):
    result, calls = publish(PUSH_MAIN_TAGS="true")
    assert result.returncode == 0, result.stderr
    docker = next(call for call in calls if call[0] == "docker")
    assert values(docker, "--cache-from") == [f"type=registry,ref={IMAGE}:buildcache"]
    assert values(docker, "--cache-to") == [f"type=registry,ref={IMAGE}:buildcache,mode=max"]
    assert f"{IMAGE}:main" in values(docker, "--tag")
    assert calls[-1] == ["python3", "ci/verify_assets_image.py", IMAGE, "inputs-test"]

    result, calls = publish(CACHE_SCOPE="refs/heads/feature", PROBE_EXISTS="true")
    assert result.returncode == 0, result.stderr
    assert calls == [["python3", "ci/verify_assets_image.py", "--exists", IMAGE, "inputs-test"]]


def test_ref_caches_are_distinct_and_never_overwrite_main(publish):
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-sim-images.yml").read_text())
    step = next(
        step for step in workflow["jobs"]["assets"]["steps"] if step.get("run") == "bash ci/build_assets_image.sh"
    )
    assert step["env"]["CACHE_SCOPE"] == "${{ github.ref }}"
    refs = ["refs/heads/feat/x", "refs/heads/feat-x", "refs/tags/feat/x", "refs/tags/main"]
    refs += ["refs/heads/" + "x" * 100 + suffix for suffix in ("a", "b")]
    exports = set()
    for ref in refs:
        result, calls = publish(CACHE_SCOPE=ref)
        assert result.returncode == 0, result.stderr
        docker = next(call for call in calls if call[0] == "docker")
        (export,) = values(docker, "--cache-to")
        tag = export.removeprefix(f"type=registry,ref={IMAGE}:").removesuffix(",mode=max")
        assert re.fullmatch(r"cache-[A-Za-z0-9_.-]+", tag) and len(tag) <= 128
        assert values(docker, "--cache-from") == [
            f"type=registry,ref={IMAGE}:buildcache",
            f"type=registry,ref={IMAGE}:{tag}",
        ]
        assert f"{IMAGE}:main" not in values(docker, "--tag")
        assert export.endswith(",mode=max")
        exports.add(export)
    assert len(exports) == len(refs)


@pytest.mark.parametrize("scope", [None, ""])
def test_non_main_requires_scope_before_external_actions(publish, scope):
    result, calls = publish(**({} if scope is None else {"CACHE_SCOPE": scope}))
    assert result.returncode != 0
    assert "CACHE_SCOPE" in result.stderr
    assert calls == []
