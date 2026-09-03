# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import ast
import functools
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dashboard import CYAN, GREEN, NC, STEP_LABEL_WIDTH, YELLOW, active_step

try:
    import tomllib as toml_parser
except ModuleNotFoundError:
    try:
        import tomli as toml_parser  # type: ignore[no-redef]
    except ModuleNotFoundError:
        toml_parser = None  # type: ignore[assignment]

SCRIPT_PATH = Path(__file__).resolve()
LAUNCHER_DIR = SCRIPT_PATH.parent
SIM_DIR = LAUNCHER_DIR.parent
REPO_ROOT = SIM_DIR.parent
ENV_PATH = REPO_ROOT / ".env"
ENV_TEMPLATE_PATH = REPO_ROOT / ".env.template"
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
SETTINGS_TEMPLATE_PATH = REPO_ROOT / "config" / "settings.yaml.template"
SIM_CONFIG_PATH = REPO_ROOT / "sim" / "config.toml"
SIM_CONFIG_TEMPLATE_PATH = REPO_ROOT / "sim" / "config.toml.template"
STATE_DIR = LAUNCHER_DIR / ".state"
LOG_DIR = STATE_DIR / "logs"
BOOTSTRAP_LOG_PATH = LOG_DIR / "bootstrap.log"
COMPOSE_LOG_PATH = LOG_DIR / "compose.log"
OS_BUILD_LOG_PATH = LOG_DIR / "os-build.log"
VIEWER_BUILD_LOG_PATH = LOG_DIR / "viewer-build.log"
WORLD_SERVER_LOG_PATH = LOG_DIR / "world-server.log"
WORLD_SERVER_PID_PATH = STATE_DIR / "world-server.pid"
# Recorded, not inferred: after a port override the configuration and the
# running server's ports are exactly what differ.
WORLD_SERVER_PORTS_PATH = STATE_DIR / "world-server.ports"
# Content digest of the model sources the running world server compiled
# (see runtime._world_model_sources_digest); written next to the pid.
WORLD_SERVER_MODEL_DIGEST_PATH = STATE_DIR / "world-server.model-digest"
OS_SESSION_LOG_PATH = LOG_DIR / "os-session.log"
DOWN_LOG_PATH = LOG_DIR / "down.log"
ROS_INSTALL_STATE_PATH = STATE_DIR / "ros-install.inputs.sha256"
OS_SESSION_READY_POLL_SECONDS = 0.25
GENERATED_OS_ENV_PATH = STATE_DIR / "innate-os.env"

PORT_BASE_ENV = "INNATE_SIM_PORT_BASE"


def _resolve_port(name: str, offset: int, classic: int) -> int:
    """Own override, else PORT_BASE_ENV plus this port's offset, else `classic`.
    Exits rather than raising: this runs at import, outside main()'s boundary."""
    for source, raw, shift in (
        (name, os.environ.get(name, "").strip(), 0),
        (PORT_BASE_ENV, os.environ.get(PORT_BASE_ENV, "").strip(), offset),
    ):
        if not raw:
            continue
        if raw.isdigit() and 1 <= int(raw) + shift <= 65535:
            return int(raw) + shift
        print(f"Error: {source}={raw!r} is not a usable port for {name}.", file=sys.stderr)
        raise SystemExit(1)
    return classic


SIM_HTTPS_PORT = _resolve_port("SIM_HTTPS_PORT", 0, 443)
SIM_HTTP_PORT = _resolve_port("SIM_HTTP_PORT", 1, 80)
SIM_ROSBRIDGE_PORT = _resolve_port("SIM_ROSBRIDGE_PORT", 2, 9090)
SIM_UDP_PORT = _resolve_port("SIM_UDP_PORT", 3, 9999)
SIM_FOXGLOVE_PORT = _resolve_port("SIM_FOXGLOVE_PORT", 4, 8765)
WORLD_SERVER_PORT = _resolve_port("SIM_WORLD_PORT", 5, 8799)
WORLD_STATE_PORT = _resolve_port("SIM_WORLD_STATE_PORT", 6, 8800)
# Injected into compose's environment, so a base-only override reaches the
# compose file without it knowing the base exists.
PUBLISHED_PORT_ENV = {
    "SIM_HTTPS_PORT": str(SIM_HTTPS_PORT),
    "SIM_HTTP_PORT": str(SIM_HTTP_PORT),
    "SIM_ROSBRIDGE_PORT": str(SIM_ROSBRIDGE_PORT),
    "SIM_UDP_PORT": str(SIM_UDP_PORT),
    "SIM_FOXGLOVE_PORT": str(SIM_FOXGLOVE_PORT),
}
# How brain_client reaches Gemini: through the Innate proxy with a service key,
# straight at Google with a Gemini key, or not at all.
INNATE_BACKEND = "innate"
GEMINI_BACKEND = "gemini"
NO_BACKEND = "none"
GEMINI_API_KEY = "GEMINI_API_KEY"
CARTESIA_API_KEY = "CARTESIA_API_KEY"
INNATE_SERVICE_KEY = "INNATE_SERVICE_KEY"
AUTO_OS_IMAGE = "auto"
LOCAL_OS_IMAGE = "local"
DEFAULT_SIM_OS_IMAGE = "ghcr.io/innate-inc/innate-os-sim-ros"
SIM_IMAGE_INPUT_FILES = (
    ".dockerignore",
    "sim/Dockerfile",
    "sim/Dockerfile.ros-prebuilt",
    "sim/Dockerfile.ros-prebuilt.dockerignore",
    "ros2_ws/apt-dependencies.common.txt",
    "ros2_ws/apt-dependencies.hardware.txt",
    "ros2_ws/apt-dependencies.sim.txt",
)
# Addressed by compute_local_image_inputs_hash, not the ROS image's hash: that
# one covers ros2_ws/src, which this image does not contain, so a source edit
# would rename an identical image out of reach.
DEFAULT_SIM_DEPS_IMAGE = "ghcr.io/innate-inc/innate-os-sim-deps"
DEPS_IMAGE_TAG_PREFIX = "deps-"
# What the local (deps-only) sim/Dockerfile build actually reads from the repo.
LOCAL_OS_IMAGE_REPO = "innate-os-sim-clean-innate"
LOCAL_IMAGE_INPUT_FILES = (
    ".dockerignore",
    "sim/Dockerfile",
    "ros2_ws/apt-dependencies.common.txt",
    "ros2_ws/apt-dependencies.sim.txt",
    "scripts/update/setup_repos.sh",
)
ROS_INSTALL_VALIDATION_INPUT_FILES = ("scripts/validate_sim_ros_install.zsh",)
# The layered asset image, resolved exactly like DEFAULT_SIM_OS_IMAGE: hash the
# inputs, pull inputs-<hash>. Every input is tracked -- sim/tools derives the
# geometry from them during the build -- so there is nothing for a lock to pin.
DEFAULT_SIM_ASSETS_IMAGE = "ghcr.io/innate-inc/innate-os-sim-assets"
# COPY order of sim/Dockerfile.assets' final stage. A manifest names no layer,
# so consumers address them by position: runtime.ensure_sim_assets fetches
# layers[index("work")], and ci/verify_assets_image.py asserts every pushed
# image matches this order (the Dockerfile cannot import it).
ASSETS_IMAGE_LAYERS = ("work", "viewer")
ASSETS_IMAGE_INPUT_FILES = (
    "sim/Dockerfile.assets",
    "sim/Dockerfile.assets.dockerignore",
    "sim/ATTRIBUTION.md",
    "sim/pyproject.toml",
    "sim/uv.lock",
    # package*.json only: npm ci for the apartment split. tsconfig, vite
    # config and src/** belong to the bundle image (DEFAULT_SIM_VIEWER_IMAGE).
    "sim/viewer/package.json",
    "sim/viewer/package-lock.json",
    "ci/build_assets_image.sh",
    "ci/seed_asset_context.py",
    "ci/verify_assets_image.py",
    # export_nav_map.py's shim onto the driver package; just this file, not
    # sim/sandbox (see the COPY in sim/Dockerfile.assets).
    "sim/sandbox/_driver_pkg.py",
)
# ros2_ws IS an input, for exactly one reason: export_nav_map.py instantiates
# VirtualMars(), so /work/map is a function of the driver and the robot's laser
# height. Unhashed, a driver edit would ride into /work/map under a tag whose
# name does not mention it. Cheap because sim/Dockerfile.assets copies ros2_ws
# in below CoACD: a driver edit re-runs the nav map (minutes), not the bake.
ASSETS_IMAGE_PATHSPECS = (
    "sim/environments",
    "sim/tools",
    "sim/viewer/tools",
    "ros2_ws/src/mars_bot/mars_sim_driver",
    "ros2_ws/src/mars_bot/mars_sim",
)
# Geometry is carved out by EXCLUSION, so a pathspec added above joins the
# geometry hash by default: wrongly excluded costs a needless refusal, wrongly
# included reuses geometry that no longer matches.
NON_GEOMETRY_PATHSPECS = (
    "ros2_ws/src/mars_bot/mars_sim_driver",
    "ros2_ws/src/mars_bot/mars_sim",
)
GEOMETRY_INPUT_PATHSPECS = tuple(p for p in ASSETS_IMAGE_PATHSPECS if p not in NON_GEOMETRY_PATHSPECS)
# The SimSession bundle the webapp loads, as its own image
# (sim/viewer/Dockerfile), addressed by inputs-<compute_viewer_inputs_hash over
# sim/viewer on disk>. That hash covers a little more than the build reads
# (README.md, tools/), so a README edit republishes a ~1 MB image -- at that
# size the over-trigger beats curating an input list, unlike the asset image's.
DEFAULT_SIM_VIEWER_IMAGE = "ghcr.io/innate-inc/innate-os-sim-viewer"
VIEWER_TREE_PATH = "sim/viewer"
# Where a locally built bundle lands when the tree is dirty. A separate repo
# name, never the published one: a local build claiming the canonical tag would
# shadow the registry's copy forever, since docker_image_present finds it first.
LOCAL_VIEWER_IMAGE_REPO = "innate-os-sim-viewer-local"
# The subtrees of the image's `work` layer, which the host installs under
# sim/assets/. Shared so both sides agree: ci/seed_asset_context.py assembles
# the build context from these and runtime.ensure_sim_assets installs from
# them, and a drift would surface only as missing geometry on a user's machine.
# (The dockerignore names them a third time and cannot import, so
# tests/test_assets_image_inputs.py holds it equal.)
#
# Split by CUSTODY: DERIVED is generated by the sim/tools pipeline during the
# build; AUTHORED props are hand-made, seeded file-by-file from pinned URLs
# (their collision hulls are derived, so deliberately not pinned).
SIM_ASSET_UNITS_DERIVED = (
    "apartment_split",
    "apartment_split_v2",
    "apartment_visual",
    "map",
)
SIM_ASSET_UNITS_AUTHORED = (
    "humans",
    "objects",
)
SIM_ASSET_UNITS = SIM_ASSET_UNITS_DERIVED + SIM_ASSET_UNITS_AUTHORED
# This file is deliberately NOT in ASSETS_IMAGE_INPUT_FILES -- that would retag
# the asset image on every unrelated launcher edit. Safe only because
# tests/test_assets_image_inputs.py holds the dockerignore (which IS hashed)
# EQUAL to this set; weaken it to a subset check and that stops being true.
OS_CONTAINER_SERVICE = "innate"
# One stack per checkout: clones used to share a container and a volume set, so
# `up` from the second served the first one's code or wiped its build. The
# compose file cannot compute this; it arrives as `-p` and
# INNATE_OS_CONTAINER_NAME.
HOST_REPO_ID = hashlib.sha256(str(REPO_ROOT.resolve()).encode("utf-8")).hexdigest()[:16]
COMPOSE_PROJECT_NAME = f"innate-os-{HOST_REPO_ID}"
OS_CONTAINER_NAME = f"innate-dev-{HOST_REPO_ID}"
# Removed once on upgrade: it holds 443/80/9090.
LEGACY_SHARED_CONTAINER = "innate-dev"
LEGACY_SHARED_PROJECT = "innate-os"
LEGACY_CLOUD_AGENT_CONTAINER = "innate-cloud-agent"
OS_CONTAINER_TMUX_CMD = "./scripts/launch_sim_in_tmux.zsh --detach"
SECRET_ENV_KEYS = (INNATE_SERVICE_KEY, GEMINI_API_KEY, CARTESIA_API_KEY)
LOG_TARGETS = {
    "bootstrap": BOOTSTRAP_LOG_PATH,
    "compose": COMPOSE_LOG_PATH,
    "os-build": OS_BUILD_LOG_PATH,
    "viewer-build": VIEWER_BUILD_LOG_PATH,
    "world-server": WORLD_SERVER_LOG_PATH,
    "os-session": OS_SESSION_LOG_PATH,
    "down": DOWN_LOG_PATH,
}

SHOW_LIVE_DASHBOARD_DEFAULT = sys.stdout.isatty()
TMUX_SESSION_NAME = "innate"
CLI_SIM = "./innate-sim"


class StackError(RuntimeError):
    pass


class DockerUnresponsiveError(StackError):
    """Docker did not answer a probe; availability is unknown, not "no"."""


def labelled(label: str, message: str, colour: str = CYAN) -> str:
    """One line in the launcher's label column, the shape every step, check and
    prompt shares. Continuation lines keep the indent rather than starting back
    at the margin, so a multi-line remedy still reads as one message."""
    indent = " " * (STEP_LABEL_WIDTH + 4)
    first, *rest = message.split("\n")
    lines = [f"  {colour}{label:>{STEP_LABEL_WIDTH}}{NC}  {first.strip()}"]
    # Only the indent this function applies: a message written before the
    # column existed carries its own leading spaces, and the two used to add up.
    lines += [f"{indent}{line.strip()}" if line.strip() else "" for line in rest]
    return "\n".join(lines)


def log(message: str) -> None:
    # Inside a live step, progress IS the step's detail; printing it as its own
    # line would scroll the spinner away one message at a time.
    step = active_step()
    if step is not None:
        step.detail = message
        return
    print(labelled("innate", message))


def success(message: str) -> None:
    _print_around_step(labelled("ok", message, GREEN))


def warn(message: str) -> None:
    _print_around_step(labelled("warn", message, YELLOW))


def _print_around_step(line: str) -> None:
    """Something worth keeping: print it above the spinner, which redraws."""
    step = active_step()
    if step is not None:
        step.note(line)
        return
    print(line)


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    shutil.copyfile(ENV_TEMPLATE_PATH, ENV_PATH)
    warn(f"Created {ENV_PATH} from template.")


def ensure_config_file(path: Path, template_path: Path) -> None:
    if path.exists():
        return
    shutil.copyfile(template_path, path)
    warn(f"Created {path} from template. Edit it only if you need non-default behavior.")


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key.strip()] = value
    return env


def is_configured_secret_value(_key: str, value: str | None) -> bool:
    if value is None:
        return False
    return bool(value.strip())


def parse_toml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if toml_parser is not None:
        with path.open("rb") as f:
            data = toml_parser.load(f)
        return data if isinstance(data, dict) else {}

    data = parse_simple_toml(path.read_text())
    return data if isinstance(data, dict) else {}


def strip_toml_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            continue
        if char == "#" and not quote:
            return line[:index]
    return line


def parse_simple_toml_value(raw_value: str) -> object:
    value = raw_value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith(("'", '"')):
        if not value.endswith(value[0]):
            raise StackError(
                "Invalid quoted TOML value in launcher config. Check that strings "
                "use matching quotes and valid escapes."
            )
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise StackError(
                "Invalid quoted TOML value in launcher config. Check that strings "
                "use matching quotes and valid escapes."
            ) from exc
    try:
        return int(value, 10)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError as exc:
        raise StackError(
            "Unsupported TOML value in launcher config. Use strings, booleans, "
            "or run with Python 3.11+/install tomli for full TOML support."
        ) from exc


def parse_simple_toml(contents: str) -> dict[str, object]:
    data: dict[str, object] = {}
    section: dict[str, object] = data

    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = data
            for part in line[1:-1].split("."):
                part = part.strip()
                if not part:
                    raise StackError(f"Invalid TOML section on line {line_number}.")
                nested = section.setdefault(part, {})
                if not isinstance(nested, dict):
                    raise StackError(f"Invalid TOML section on line {line_number}.")
                section = nested
            continue
        if "=" not in line:
            raise StackError(f"Invalid TOML assignment on line {line_number}.")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise StackError(f"Invalid TOML key on line {line_number}.")
        section[key] = parse_simple_toml_value(raw_value)

    return data


def get_nested_value(data: dict[str, object], *keys: str) -> object | None:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def get_nested_str(data: dict[str, object], *keys: str) -> str | None:
    value = get_nested_value(data, *keys)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def get_nested_bool(data: dict[str, object], *keys: str) -> bool | None:
    value = get_nested_value(data, *keys)
    if isinstance(value, bool):
        return value
    return None


def resolve_brain_backend(env: dict[str, str]) -> str:
    """Which key the in-process brain (brain_client) will use to reach Gemini.

    The service key wins: it also buys voice, which a Gemini key does not.
    brain_client's `Backend` (brain/transport.py) makes the real choice and owns
    this precedence; the launcher runs on the host and cannot import it, so this
    restates the rule. Change one and change the other.
    """
    if is_configured_secret_value(INNATE_SERVICE_KEY, env.get(INNATE_SERVICE_KEY, "")):
        return INNATE_BACKEND
    if is_configured_secret_value(GEMINI_API_KEY, env.get(GEMINI_API_KEY, "")):
        return GEMINI_BACKEND
    return NO_BACKEND


def require_path(path: Path, label: str) -> Path:
    if not path.exists():
        raise StackError(f"{label} not found at {path}")
    return path


def git_tracked_files(repo_root: Path, *pathspecs: str, include_untracked: bool = False) -> list[Path]:
    """Repo-relative paths of the git-tracked files under the pathspecs;
    include_untracked adds files git does not track but does not ignore
    either. Errors loudly rather than falling back to a filesystem walk --
    see _collect_input_files for why the tracked set is the hashed set.
    """
    others = ["--others", "--exclude-standard"] if include_untracked else []
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", *others, "--", *pathspecs],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(name) for name in result.stdout.split("\0") if name]


def _collect_input_files(
    repo_root: Path,
    static_files: tuple[str, ...] = (),
    pathspecs: tuple[str, ...] = (),
    *,
    include_untracked: bool = False,
) -> list[Path]:
    """Whichever `static_files` exist, plus every tracked file under `pathspecs`.

    Tracked files, NOT a filesystem walk: rglob would also pull in untracked/
    gitignored cruft (.DS_Store, build artifacts) that a dev's tree has but
    CI's clean checkout does not, making the content-addressed tag
    non-reproducible. include_untracked widens to the working tree, still minus
    gitignore -- right only for the viewer bundle, whose hash is SUPPOSED to
    match no published image when the tree is dirty.
    """
    relative_paths = [Path(p) for p in static_files if (repo_root / p).is_file()]
    # One git invocation for all pathspecs: `git ls-files` takes them together.
    found = git_tracked_files(repo_root, *pathspecs, include_untracked=include_untracked) if pathspecs else []
    for relative_path in found:
        if "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
            continue
        if (repo_root / relative_path).is_file():
            relative_paths.append(relative_path)
    return sorted(set(relative_paths), key=lambda path: path.as_posix())


def _hash_input_files(digest: hashlib._Hash, repo_root: Path, relative_paths: list[Path]) -> None:
    """Fold `<repo-relative posix path>\0<contents>\0` per file into `digest`.

    This framing IS the identity of every published image tag, so it lives in
    exactly one place: an edit here must not be able to rename one family of
    images and not the others.
    """
    for relative_path in relative_paths:
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((repo_root / relative_path).read_bytes())
        digest.update(b"\0")


def iter_sim_image_input_files(repo_root: Path) -> list[Path]:
    return _collect_input_files(repo_root, SIM_IMAGE_INPUT_FILES, ("ros2_ws/src",))


# The compute_*_hash functions are cached: each is asked repeatedly per command,
# costs a git spawn plus megabytes of file reads, and cannot change in-process.
@functools.lru_cache
def compute_sim_image_inputs_hash(repo_root: Path) -> str:
    digest = hashlib.sha256()
    _hash_input_files(digest, repo_root, iter_sim_image_input_files(repo_root))
    return digest.hexdigest()


def compute_ros_install_validation_hash(repo_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(compute_sim_image_inputs_hash(repo_root).encode("utf-8"))
    relative_paths = [Path(raw_path) for raw_path in ROS_INSTALL_VALIDATION_INPUT_FILES]
    for relative_path in relative_paths:
        if not (repo_root / relative_path).is_file():
            raise StackError(f"ROS install validation script is missing: {relative_path}")
    _hash_input_files(digest, repo_root, relative_paths)
    return digest.hexdigest()


def iter_assets_image_input_files(repo_root: Path) -> list[Path]:
    """Tracked files that determine the asset image's contents.

    Same git-tracked-only rule as iter_sim_image_input_files, for the same
    reason. tests/test_assets_image_inputs.py checks that publish-sim-images.yml
    actually rebuilds when one of these changes.
    """
    return _collect_input_files(repo_root, ASSETS_IMAGE_INPUT_FILES, ASSETS_IMAGE_PATHSPECS)


@functools.lru_cache
def compute_assets_image_inputs_hash(repo_root: Path) -> str:
    """The tracked files the asset image is built from, and nothing else."""
    digest = hashlib.sha256()
    _hash_input_files(digest, repo_root, iter_assets_image_input_files(repo_root))
    return digest.hexdigest()


def resolve_assets_image(repo_root: Path) -> str:
    return f"{DEFAULT_SIM_ASSETS_IMAGE}:inputs-{compute_assets_image_inputs_hash(repo_root)}"


@functools.lru_cache
def compute_geometry_inputs_hash(repo_root: Path) -> str:
    """Whether the geometry already installed still describes this checkout.

    A second hash, not a second identity: a driver edit rightly renames the
    asset image, but must not hard-stop `up` -- geometry has no local build path
    to fall back to, and a fork has no CI to publish a new one.
    """
    digest = hashlib.sha256()
    _hash_input_files(
        digest,
        repo_root,
        _collect_input_files(repo_root, ASSETS_IMAGE_INPUT_FILES, GEOMETRY_INPUT_PATHSPECS),
    )
    return digest.hexdigest()


@functools.lru_cache
def compute_viewer_inputs_hash(repo_root: Path) -> str:
    """Content hash of sim/viewer as it is ON DISK, tracked or not.

    One hash for both the published bundle image and the local one: an edited
    file renames the image to one CI cannot have published, so the launcher
    builds it; a clean checkout hashes exactly what CI hashed. Membership is a
    superset of what the build COPYs -- over-reacting costs one cache-warm
    rebuild, under-reacting serves a stale bundle with no sign of it.
    """
    digest = hashlib.sha256()
    _hash_input_files(digest, repo_root, _viewer_input_files(repo_root))
    return digest.hexdigest()


def _viewer_input_files(repo_root: Path) -> list[Path]:
    try:
        return _collect_input_files(repo_root, pathspecs=(VIEWER_TREE_PATH,), include_untracked=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StackError(
            f"Cannot name the sim viewer bundle image: `git ls-files` failed in {repo_root}.\n"
            "Its tag is a hash of sim/viewer, so this needs a git checkout. Set "
            "INNATE_SIM_VIEWER_BUNDLE_IMAGE to name one explicitly if you have no git here."
        ) from exc


def viewer_tree_dirty(repo_root: Path) -> bool:
    """Does sim/viewer differ from HEAD? Not a second hash -- a cheap,
    network-free "this cannot have been published" short-circuit that saves
    the registry probe. compute_viewer_inputs_hash already names a dirty tree
    correctly on its own.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", VIEWER_TREE_PATH],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def resolve_viewer_image(repo_root: Path) -> str:
    return f"{DEFAULT_SIM_VIEWER_IMAGE}:inputs-{compute_viewer_inputs_hash(repo_root)}"


def resolve_local_viewer_image(repo_root: Path) -> str:
    """Same hash, different repo -- see LOCAL_VIEWER_IMAGE_REPO."""
    return f"{LOCAL_VIEWER_IMAGE_REPO}:inputs-{compute_viewer_inputs_hash(repo_root)}"


def resolve_auto_os_image(repo_root: Path) -> str:
    return f"{DEFAULT_SIM_OS_IMAGE}:inputs-{compute_sim_image_inputs_hash(repo_root)}"


def compute_local_image_inputs_hash(repo_root: Path) -> str:
    """Hash of only what the local sim/Dockerfile build actually consumes.

    Unlike the prebuilt image (which bakes the colcon workspace and rightly
    hashes all of ros2_ws/src), the local fallback is deps-only -- source is
    bind-mounted and built in-container. Hashing source here made every code
    edit rename an identical image: a doomed pull attempt, a no-op rebuild, a
    recreated container, and one more stale tag per edit.
    """
    digest = hashlib.sha256()
    _hash_input_files(digest, repo_root, [Path(p) for p in LOCAL_IMAGE_INPUT_FILES if (repo_root / p).is_file()])
    return digest.hexdigest()


def resolve_local_os_image(repo_root: Path) -> str:
    """Content-addressed tag for the local fallback build, so checkouts with
    different image inputs (Dockerfile, apt lists) keep separate images
    instead of clobbering a shared :latest."""
    return f"{LOCAL_OS_IMAGE_REPO}:inputs-{compute_local_image_inputs_hash(repo_root)}"


def resolve_deps_image(repo_root: Path) -> str:
    """The published twin of resolve_local_os_image: same Dockerfile, same
    inputs, same hash. Its own prefix, because the ROS image alongside it names
    a different hash under `inputs-`."""
    return f"{DEFAULT_SIM_DEPS_IMAGE}:{DEPS_IMAGE_TAG_PREFIX}{compute_local_image_inputs_hash(repo_root)}"


def resolve_os_image_setting(value: str | None, repo_root: Path) -> tuple[str, bool]:
    if value is None or value == AUTO_OS_IMAGE:
        return resolve_auto_os_image(repo_root), True
    if value == LOCAL_OS_IMAGE:
        return "", False
    return value, False


def get_config() -> dict[str, object]:
    ensure_env_file()
    ensure_config_file(SETTINGS_PATH, SETTINGS_TEMPLATE_PATH)
    ensure_config_file(SIM_CONFIG_PATH, SIM_CONFIG_TEMPLATE_PATH)

    user_env = parse_env_file(ENV_PATH)
    raw_env = dict(user_env)
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if is_configured_secret_value(key, value):
            raw_env[key] = value
    sim_config = parse_toml_file(SIM_CONFIG_PATH)
    if "cloud_agent" in sim_config:
        # An existing config.toml is never rewritten (and `clean` preserves it),
        # so the dead section outlives the code that validated it.
        warn(
            f"{SIM_CONFIG_PATH} still has a [cloud_agent] section. It is ignored -- the brain now runs "
            "inside brain_client. Delete the section to silence this."
        )

    merged_env = dict(raw_env)
    # 9090 is rosbridge's port INSIDE the container, which no override moves.
    merged_env.setdefault("ROSBRIDGE_URI", "ws://localhost:9090")

    os_repo = require_path(REPO_ROOT, "innate-os repository")
    sim_repo = require_path(REPO_ROOT / "sim", "sim repository")

    os_always_build = get_nested_bool(sim_config, "os", "always_build")
    os_pull_image = get_nested_bool(sim_config, "os", "pull_image")
    configured_os_image = get_nested_str(sim_config, "os", "image")
    env_os_image = os.environ.get("INNATE_OS_IMAGE", "").strip() or None
    os_image, os_image_auto = resolve_os_image_setting(
        configured_os_image or env_os_image,
        os_repo,
    )

    return {
        "raw_env": merged_env,
        "user_env": user_env,
        "brain_backend": resolve_brain_backend(merged_env),
        "os_repo": os_repo,
        "sim_repo": sim_repo,
        "foxglove_port": str(SIM_FOXGLOVE_PORT),
        "webapp_url": "https://localhost" + ("" if SIM_HTTPS_PORT == 443 else f":{SIM_HTTPS_PORT}"),
        "rosbridge_url": f"ws://localhost:{SIM_ROSBRIDGE_PORT}",
        "os_image": os_image,
        "os_image_auto": os_image_auto,
        "os_pull_image": os_pull_image if os_pull_image is not None else True,
        "os_always_build": os_always_build if os_always_build is not None else False,
    }


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in sorted(values.items()) if value != ""]
    path.write_text("\n".join(lines) + "\n")


def build_os_env(config: dict[str, object]) -> Path:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    os_env: dict[str, str] = dict(raw_env)

    ensure_state_dir()
    write_env_file(GENERATED_OS_ENV_PATH, os_env)
    return GENERATED_OS_ENV_PATH


if __name__ == "__main__":
    # `python3 sim/launcher/config.py <which>-hash`, for CI.
    #
    # The tag CI PUSHES and the tag the launcher PULLS must be the same string,
    # so CI asks the launcher rather than carrying its own copy of each rule.
    # Named, not argless: there are several hashes in this file.
    _hash_commands = {
        "assets-image-hash": compute_assets_image_inputs_hash,
        "deps-image-hash": compute_local_image_inputs_hash,
        "sim-image-hash": compute_sim_image_inputs_hash,
        "viewer-image-hash": compute_viewer_inputs_hash,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in _hash_commands:
        raise SystemExit("usage: config.py " + "|".join(_hash_commands))
    print(_hash_commands[sys.argv[1]](REPO_ROOT))
