# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill catalog: discovery, metadata, publishing, reload, and physical skills.

Code skills are stored as classes plus metadata harvested at discovery — never
live instances; the skills server constructs a fresh instance per run, so a
reload here is just an entry swap.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args, get_origin

import h5py
from brain_messages.msg import AvailableSkills, SkillInfo
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

from brain_client.common.dynamic_loader import class_name_to_snake_case, evict_modules_under
from brain_client.common.script_paths import (
    LEARNED_GROUP,
    classify_source,
    ensure_user_directories,
    get_custom_skills_dir,
    get_innate_skills_dir,
    get_learned_skills_dir,
    get_skill_directories,
    get_workspace_dir,
    is_draft,
    skill_id_prefix_for,
)
from brain_client.skills.hot_reload_watcher import HotReloadWatcher
from brain_client.skills.physical import (
    get_episode_count,
    has_physical_metadata,
    shutdown_quietly,
    validate_physical_skill,
)
from brain_client.skills.physical_refs import (
    prune_dir_shims,
    render_dir_shims,
    render_refs,
    write_dir_shims,
    write_refs,
)
from brain_client.skills.replay_conversion import recording_action_to_replay
from brain_client.skills.workspace_import import (
    format_load_error,
    import_workspace_packages,
    module_skill_id,
    registered_workspace_skills,
)


def _annotation_is_float(annotation) -> bool:
    """True if a param annotation is ``float`` (or a union including it).

    Handles both real type objects and string annotations (skills using
    ``from __future__ import annotations`` expose ``"float"`` instead).
    """
    if annotation is float or annotation == "float":
        return True
    return any(arg is float or arg == "float" for arg in get_args(annotation))


@dataclass(frozen=True)
class CodeSkillEntry:
    """A discovered code skill: its class plus metadata harvested at discovery.
    execute() is introspected exactly once — the published schema, input
    coercion and invoker validation all read these fields."""

    display_name: str
    skill_class: type
    source: str  # "shipped" | "user" — stamped onto each run's instance
    group: str  # folder path inside the package ("" = root) — UI grouping only
    guidelines: str
    guidelines_when_running: str
    inputs: dict  # {param: schema} from execute()'s signature
    inputs_json: str  # the same schema, serialized for the roster message
    float_params: frozenset[str]  # params to widen int -> float before dispatch
    accepts_extra_inputs: bool  # execute() takes **kwargs (compat plumbing)
    draft: bool  # on trial by learn_skill: runnable by id, withheld from the roster


@dataclass(frozen=True)
class PhysicalSkillEntry:
    """A discovered physical skill: its metadata.json plus validation results.

    No episode count here: episodes accumulate while a skill trains, so the
    roster re-reads them at publish time (_build_physical_skill_info)."""

    metadata: dict
    directory: str
    in_training: bool


class SkillRepository:
    def __init__(self, node):
        self._node = node
        self._logger = node.get_logger()

        self._skills_directories = self._resolve_skills_directories()

        # guards the skill dicts against the HotReloadWatcher thread
        self._skills_lock = threading.Lock()
        self._code_skills: dict[str, CodeSkillEntry] = {}
        self._physical_skills: dict[str, PhysicalSkillEntry] = {}
        self._in_training_skills: dict[str, PhysicalSkillEntry] = {}
        # skill_id -> load-error text for files that failed to load. Published on
        # the roster (SkillInfo.load_error) so a broken skill shows up in the UI
        # with its error instead of silently vanishing. Never registered with the
        # cloud agent and not runnable.
        self._broken_skills: dict[str, str] = {}
        # module name -> skill ids it registered on its last clean import,
        # carried across reloads: a module that later fails to import rosters
        # every skill it used to define as broken (see _load_code_skills).
        self._skill_ids_by_module: dict[str, list[str]] = {}

        # Evict before the first load too: in a fresh process this is a no-op,
        # but it makes construction deterministic when workspace modules are
        # already cached (tests, re-instantiation) — same path as reload_all.
        self._evict_workspace_modules()
        # Physical first, refs second, code last: skills `from physical_skills
        # import X`, and on a fresh workspace that package doesn't exist until
        # we write it — importing code first would roster every such skill
        # broken for the whole first pass.
        self._physical_skills, self._in_training_skills, physical_broken = self._load_physical_skills(
            self._skills_directories
        )
        self._refresh_physical_refs(self._physical_skills, self._in_training_skills)
        self._code_skills, code_broken = self._load_code_skills()
        self._logger.info(f"Successfully loaded {len(self._code_skills)} code skills")
        self._broken_skills = {**code_broken, **physical_broken}
        self._logger.info(f"Successfully loaded {len(self._physical_skills)} physical skills")
        self._logger.info(f"Found {len(self._in_training_skills)} in-training skills")
        if self._broken_skills:
            self._logger.warning(f"{len(self._broken_skills)} skills failed to load: {list(self._broken_skills)}")

        qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        self._skills_publisher = node.create_publisher(AvailableSkills, "/brain/available_skills", qos)
        # kept for the heartbeat: rws subscribes after boot and never gets the
        # latched sample, so periodic re-publish is what reaches the webapp
        self._last_published_msg: AvailableSkills | None = None

        self._hot_reload_watcher = None

    # --- counts / thread-safe accessors (used by executor + service messages) ---
    @property
    def code_count(self) -> int:
        return len(self._code_skills)

    @property
    def physical_count(self) -> int:
        return len(self._physical_skills)

    def get_code_skill(self, skill_id: str) -> CodeSkillEntry | None:
        with self._skills_lock:
            return self._code_skills.get(skill_id)

    def get_physical_skill(self, skill_id: str) -> PhysicalSkillEntry | None:
        with self._skills_lock:
            return self._physical_skills.get(skill_id)

    def bare_id_candidates(self, skill_id: str) -> list[str]:
        """Ids a possibly-bare name may resolve to, in precedence order.

        ``local/`` beats ``innate-os/`` (a user override wins over the shipped
        skill of the same name), then any dropped-in package that could hold
        the name. Already-qualified ids resolve only to themselves.
        """
        if "/" in skill_id:
            return [skill_id]
        candidates = [f"local/{skill_id}", f"innate-os/{skill_id}"]
        with self._skills_lock:
            live = [*self._code_skills, *self._physical_skills, *self._broken_skills]
        for namespace in dict.fromkeys(i.split("/", 1)[0] for i in live if "/" in i):
            if namespace not in ("local", "innate-os"):
                candidates.append(f"{namespace}/{skill_id}")
        return candidates

    def get_load_error(self, skill_id: str) -> str | None:
        """The load error for a broken skill, resolving bare names like the
        invoker does (see :meth:`bare_id_candidates`). None if not broken."""
        candidates = self.bare_id_candidates(skill_id)
        with self._skills_lock:
            for candidate in candidates:
                error = self._broken_skills.get(candidate)
                if error is not None:
                    return error
            # A broken module in a subpackage rosters by its dotted module
            # path (custom_skills/boards/chess.py -> local/boards.chess),
            # which no candidate ever spells — the working class's id was
            # local/chess. Match by namespace + leaf module name so a call to
            # that id surfaces the load error instead of "unknown skill".
            # Several same-leaf broken modules (boards/chess.py AND
            # demos/chess.py) are ambiguous — report all of them rather than
            # quoting whichever iterates first and pointing the author at a
            # file they never touched.
            for candidate in candidates:
                namespace, _, leaf = candidate.partition("/")
                matches = {
                    broken_id: error
                    for broken_id, error in self._broken_skills.items()
                    if broken_id.partition("/")[0] == namespace
                    and broken_id.partition("/")[2].rpartition(".")[2] == leaf
                }
                if len(matches) == 1:
                    return next(iter(matches.values()))
                if matches:
                    return "; ".join(f"{broken_id}: {error}" for broken_id, error in sorted(matches.items()))
        return None

    def in_training_reason(self, skill_id: str) -> str | None:
        """The in-training entry a bare or qualified id matches: on the
        roster, but its runnable data (checkpoint or recorded trajectory)
        isn't on disk yet. Resolves bare names like the invoker (see
        :meth:`bare_id_candidates`). None if no candidate is training."""
        candidates = self.bare_id_candidates(skill_id)
        with self._skills_lock:
            for candidate in candidates:
                if candidate in self._in_training_skills:
                    return f"'{candidate}' has no runnable data yet (still training, or its recording is not on this robot)"
        return None

    def unavailable_reason(self, skill_id: str) -> str | None:
        """Why an id that matches *something* on the roster cannot run: a
        broken module's load error, an in-training skill's missing
        checkpoint, or both (a broken local entry shadowing a training skill
        of the same bare name — without the training note, the load error
        alone points the user at the wrong skill). Phrased to read after
        "Skill '<id>' ...". None when the id matches nothing (the caller
        says "unknown skill")."""
        load_error = self.get_load_error(skill_id)
        training = self.in_training_reason(skill_id)
        if load_error and training:
            return f"failed to load: {load_error} (note: {training})"
        if load_error:
            return f"failed to load: {load_error}"
        if training:
            return f"is not runnable: {training}"
        return None

    def all_skill_ids(self) -> list[str]:
        with self._skills_lock:
            return list(self._code_skills.keys()) + list(self._physical_skills.keys())

    # --- watcher ---
    def start_watcher(self) -> None:
        # Every callback is a full reload now, so the workspace root (recursive
        # — covers every package, incl. ones dropped in after boot) is the only
        # watch that needs scheduling. The skill dirs are still passed: a
        # physical skill is *data*, so its reload trigger is a metadata.json or
        # episode_*.h5 write, and only skills_directories resolves a non-.py
        # path to the skill that owns it. The watcher skips scheduling the ones
        # the root already covers.
        self._hot_reload_watcher = HotReloadWatcher(
            logger=self._logger,
            skills_directories=self._skills_directories,
            agents_directories=[],  # SAS doesn't handle agents
            on_reload=self._on_skills_file_changed,
            debounce_seconds=1.0,
            recursive=True,  # physical skills live in subdirs (metadata.json + assets)
            workspace_roots=[str(get_workspace_dir())],
        )
        self._hot_reload_watcher.start()

    def stop_watcher(self) -> None:
        if self._hot_reload_watcher is not None:
            self._hot_reload_watcher.stop()

    def _on_skills_file_changed(self, skill_names: list, _agent_names: list) -> None:
        """Watcher callback. Every change is a full reload under the import
        model — reload_all re-resolves directories (new drop-in packages
        included), evicts the workspace subtree, and re-imports."""
        self._logger.info(f"Hot reload triggered (skills hint: {skill_names or 'n/a'}) - full reload")
        self.reload_all()

    # --- loading ---
    def _load_code_skills(self) -> tuple[dict[str, CodeSkillEntry], dict[str, str]]:
        """Returns ``(loaded entries, broken: skill_id -> load-error text)``.

        Workspace packages are *imported* — defining a Skill subclass registers
        it (see workspace_import.py). The distinction between skill, helper,
        and broken is therefore exact, not guessed: a module that raises is
        broken (keyed by its module name), a module that imports clean and
        registers nothing is a helper, and each registered class is a skill.
        """
        broken: dict[str, str] = {}
        import_errors = import_workspace_packages(self._logger)
        # Aggregate by skill id: same-name skills in different namespaces are
        # distinct entries — display-name collisions are resolved at publish
        # time by _dedupe_display_names, not by silently dropping one here.
        id_keyed, function_local_broken = registered_workspace_skills(self._logger)
        broken.update(function_local_broken)

        # A failed import leaves no classes to key broken entries by, so a
        # multi-skill module would otherwise collapse to one module-derived
        # row — every other skill in the file vanishing from the roster with
        # no error. Roster the ids the module registered on its last clean
        # import instead; the module-derived row is only for modules that
        # never imported cleanly in this process.
        ids_by_module: dict[str, list[str]] = {}
        for skill_id, (_class_name, cls, _src_path) in id_keyed.items():
            ids_by_module.setdefault(cls.__module__, []).append(skill_id)
        for module_name, error in import_errors.items():
            known_ids = self._skill_ids_by_module.get(module_name)
            if known_ids:
                ids_by_module[module_name] = known_ids  # carry through the breakage
                for skill_id in known_ids:
                    broken[skill_id] = error
            else:
                broken[module_skill_id(module_name)] = error
        self._skill_ids_by_module = ids_by_module

        self._logger.info(f"Discovered skills: {list(id_keyed.keys())}")

        code_skills: dict[str, CodeSkillEntry] = {}
        for skill_id, (_class_name, skill_class, src_path) in id_keyed.items():
            try:
                code_skills[skill_id] = self._harvest_entry(skill_id, skill_class, src_path)
            except Exception as e:
                broken[skill_id] = format_load_error(e)
                self._logger.error(f"Error loading skill {skill_id}: {broken[skill_id]}")
        return code_skills, broken

    def _harvest_entry(self, skill_id: str, skill_class: type, src_path) -> CodeSkillEntry:
        """Build a skill's metadata from one throwaway instance — name and
        guidelines() are instance-level API (0.6.0 contract)."""
        instance = skill_class(self._logger)
        try:
            try:
                display_name = str(instance.name)
            except Exception:  # noqa: BLE001 — a broken .name property must not hide the skill
                display_name = class_name_to_snake_case(skill_class.__name__)
            inputs, float_params, accepts_extra = self._inspect_skill_inputs(skill_id, instance)
            try:
                inputs_json = json.dumps(inputs)
            except (TypeError, ValueError) as e:
                self._logger.error(f"Could not serialize inputs for code skill '{skill_id}': {e}; using empty")
                inputs, inputs_json = {}, "{}"
            entry = CodeSkillEntry(
                display_name=display_name,
                skill_class=skill_class,
                source=classify_source(src_path),
                # the folder IS the group: innate_skills.chess.x -> "chess"
                group="/".join(skill_class.__module__.split(".")[1:-1]),
                guidelines=self._safe_skill_string(skill_id, instance, "guidelines"),
                guidelines_when_running=self._safe_skill_string(skill_id, instance, "guidelines_when_running"),
                inputs=inputs,
                inputs_json=inputs_json,
                float_params=float_params,
                accepts_extra_inputs=accepts_extra,
                draft=is_draft(src_path),
            )
            declared = instance.describe_feeds()
            self._logger.info(
                f"Loaded code skill: {skill_id} ({display_name}) [source={entry.source}]"
                + (f" [declares: {declared}]" if declared else "")
            )
            # Suspicious bare annotations (typo'd feed declarations) recorded
            # at class creation — surface them here, at load/reload time,
            # where an author debugging "why is my feed None" will look.
            for issue in getattr(skill_class, "_declaration_issues", ()):
                self._logger.warning(f"{Path(src_path).name}: {issue}")
            return entry
        finally:
            # A legacy __init__ may own ROS entities; never let the throwaway leak.
            shutdown_quietly(instance, self._logger)

    def _read_physical_skill(self, skill_dir: str) -> PhysicalSkillEntry | None:
        """metadata.json -> validated entry, or None (with a log) if invalid."""
        metadata_path = os.path.join(skill_dir, "metadata.json")
        with open(metadata_path) as f:
            metadata = json.load(f)
        if not isinstance(metadata, dict):
            self._logger.warn(f"Skipped {metadata_path}: top-level JSON is {type(metadata).__name__}, expected object")
            return None
        is_valid, is_in_training = validate_physical_skill(skill_dir, metadata, self._logger)
        if not is_valid:
            self._logger.warn(f"Skipped invalid physical skill: {skill_dir}")
            return None
        return PhysicalSkillEntry(
            metadata=metadata,
            directory=skill_dir,
            in_training=is_in_training,
        )

    def _reload_physical_skill(self, skill_id: str) -> bool:
        """Re-read one physical skill's directory into the live dicts.

        Used by create_physical_skill: a full reload_all() takes seconds and
        overruns the bridge's service timeout.
        """
        basename = skill_id.split("/", 1)[-1]
        for skills_directory in self._skills_directories:
            skill_path = os.path.join(skills_directory, basename)
            if not has_physical_metadata(skill_path):
                continue
            try:
                entry = self._read_physical_skill(skill_path)
            except Exception as e:
                self._logger.error(f"Error reloading physical skill {skill_id}: {e}")
                return False
            if entry is None:
                return False
            with self._skills_lock:
                live, retired = (
                    (self._in_training_skills, self._physical_skills)
                    if entry.in_training
                    else (self._physical_skills, self._in_training_skills)
                )
                live[skill_id] = entry
                retired.pop(skill_id, None)
                self._broken_skills.pop(skill_id, None)
            self._logger.info(f"Reloaded physical skill: {skill_id}")
            return True
        return False

    def _load_physical_skills(self, skills_directories):
        """Returns ``(physical, in_training, broken: skill_id -> load-error text)``."""
        physical_skills = {}
        in_training_skills = {}
        broken: dict[str, str] = {}
        for skills_directory in skills_directories:
            if not os.path.exists(skills_directory):
                continue
            for item in os.listdir(skills_directory):
                # __pycache__/.git and friends are never skills, whatever files
                # happen to sit inside them.
                if item.startswith((".", "__")):
                    continue
                item_path = os.path.join(skills_directory, item)
                if not os.path.isdir(item_path):
                    continue
                if not has_physical_metadata(item_path):
                    continue
                skill_id = self._compute_skill_id(Path(item_path))
                try:
                    entry = self._read_physical_skill(item_path)
                    if entry is None:
                        continue
                    kind = "in-training" if entry.in_training else "physical"
                    target = in_training_skills if entry.in_training else physical_skills
                    target[skill_id] = entry
                    self._logger.info(
                        f"Loaded {kind} skill: {skill_id} (type: {entry.metadata.get('type', 'unknown')})"
                    )
                except json.JSONDecodeError as e:
                    self._logger.error(f"Skipped {item_path}: invalid JSON ({e})")
                    broken[skill_id] = f"metadata.json is invalid JSON: {e}"
                except OSError as e:
                    self._logger.error(f"Skipped {item_path}: read failed ({e})")
                    broken[skill_id] = f"read failed: {e}"
                except Exception as e:
                    self._logger.error(f"Skipped physical skill at {item_path}: {e}")
                    broken[skill_id] = f"{type(e).__name__}: {e}"
        return physical_skills, in_training_skills, broken

    def _resolve_skills_directories(self) -> list[str]:
        innate_skills_dir = str(get_innate_skills_dir())
        if not os.path.exists(innate_skills_dir):
            self._logger.fatal(f"Skills directory not found: {innate_skills_dir}")
            raise FileNotFoundError(f"Skills directory must exist at {innate_skills_dir}")
        ensure_user_directories()
        directories = [str(p) for p in get_skill_directories()]
        for directory in directories:
            self._logger.debug(f"Scanning skills directory: {directory}")
        return directories

    def _compute_skill_id(self, path: str | Path) -> str:
        """Id for a path-identified entry: physical skill dirs and legacy-lane
        code files. Workspace code skills get ids from their class instead
        (workspace_import.skill_id_for_class)."""
        p = Path(path)
        basename = p.stem if p.suffix == ".py" else p.name
        return f"{skill_id_prefix_for(path)}/{basename}"

    # --- reload ---
    def _evict_workspace_modules(self) -> None:
        """Drop every cached module under workspace/ (skills and helpers, via
        any import form) plus helper modules in legacy skill dirs. Re-import
        happens in _load_code_skills."""
        workspace_root = get_workspace_dir().resolve()
        legacy = [d for d in self._skills_directories if not Path(d).resolve().is_relative_to(workspace_root)]
        evicted = evict_modules_under([str(get_workspace_dir()), *legacy])
        if evicted:
            self._logger.info(f"Evicted workspace modules for reload: {evicted}")

    def reload_all(self) -> None:
        self._logger.info("Reloading skills...")
        self._skills_directories = self._resolve_skills_directories()
        self._evict_workspace_modules()
        # Physical before code, refs in between — see __init__ for why.
        new_physical, new_in_training, physical_broken = self._load_physical_skills(self._skills_directories)
        self._refresh_physical_refs(new_physical, new_in_training)
        new_code_skills, code_broken = self._load_code_skills()
        with self._skills_lock:
            self._code_skills = new_code_skills
            self._physical_skills = new_physical
            self._in_training_skills = new_in_training
            self._broken_skills = {**code_broken, **physical_broken}
        self._logger.info(f"Reloaded {len(new_code_skills)} code + {len(new_physical)} physical skills")
        self.publish_skills_list()

    def reload_selective(self, skill_ids: list[str]) -> list[str]:
        """Reload skills. Under the import model every reload is a full one —
        imports are cached, module identity is exact, and partial reloads were
        the source of the flat/folder, stem-resolution, and stale-helper bug
        family. ``skill_ids`` is accepted for service compatibility; the reply
        lists everything now live.
        """
        if skill_ids:
            self._logger.info(f"Selective reload requested for {skill_ids} — performing a full reload")
        self.reload_all()
        with self._skills_lock:
            return list(self._code_skills.keys()) + list(self._physical_skills.keys())

    @staticmethod
    def _slugify(display_name: str) -> str:
        """Derive a filesystem-safe directory slug from a display name ('' if none)."""
        cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", display_name)
        return re.sub(r"\s+", "-", cleaned).strip("-").lower()

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        """Write JSON to ``path`` atomically (tmp file + fsync + os.replace)."""
        path = Path(path)
        tmp_path = path.with_name(f"{path.name}.tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def create_physical_skill(self, display_name: str, kind: str = "learned") -> tuple[bool, str, str, str]:
        """Create a physical-skill directory with metadata.json; returns
        (ok, msg, dir, id). An "eval" dataset is never trained or run, so it
        gets no execution config."""
        try:
            display_name = display_name.strip()
            if not display_name:
                return False, "Skill name cannot be empty.", "", ""

            kind = (kind or "learned").strip().lower()
            if kind not in ("learned", "replay", "eval"):
                kind = "learned"

            dir_name = self._slugify(display_name)
            if not dir_name:
                return False, f"Cannot derive valid directory name from '{display_name}'.", "", ""

            skill_dir = os.path.join(str(get_custom_skills_dir()), dir_name)
            if os.path.exists(os.path.join(skill_dir, "metadata.json")):
                self._logger.info(f"Skill '{display_name}' already exists at {skill_dir}. Returning existing.")
                return True, f"Skill already exists at {skill_dir}.", skill_dir, self._compute_skill_id(skill_dir)

            os.makedirs(skill_dir, exist_ok=True)
            execution = (
                {}
                if kind in ("replay", "eval")
                else {
                    "duration": None,
                    "progress_threshold": None,
                    "start_pose": None,
                    "end_pose": None,
                    "n_action_steps": None,
                    "auto_stop": None,
                    "min_duration": None,
                    "progress_ema_alpha": None,
                    "engage_below": None,
                    "stable_min": None,
                    "stable_seconds": None,
                }
            )
            metadata = {
                "name": display_name,
                "type": kind,
                "guidelines": "",
                "guidelines_when_running": "",
                "inputs": {},
                "execution": execution,
            }
            self._write_json_atomic(Path(skill_dir) / "metadata.json", metadata)

            self._logger.info(f"Created physical skill '{display_name}' at {skill_dir}")
            # Register just this draft, not a full reload_all() — the latter takes
            # seconds and overruns the bridge's service timeout ("Service call failed").
            skill_id = self._compute_skill_id(skill_dir)
            self._reload_physical_skill(skill_id)
            self.publish_skills_list()
            return True, f"Created skill '{display_name}' at {skill_dir}.", skill_dir, skill_id
        except Exception as e:
            self._logger.error(f"Error creating physical skill: {e}")
            return False, f"Failed to create skill: {e}", "", ""

    def save_recording_as_replay_skill(
        self, task_directory: str, display_name: str, guidelines: str = "", episode_id: int = 0
    ) -> tuple[str, str, bool]:
        """Promote a recorded teleop episode into a replay skill under
        custom_skills/, dropping the raw recording. Returns
        (skill_dir, skill_id, wheeled)."""
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("Skill name cannot be empty.")

        recorder_meta, episode_path = self._resolve_recording_episode(task_directory, episode_id)
        with h5py.File(episode_path, "r") as f:
            # head_command is its own dataset (replay-only, never in /action); older
            # recordings without it just yield an arm+base trajectory. The casts
            # narrow h5py's Group | Dataset | Datatype lookup union.
            head = cast(h5py.Dataset, f["head_command"])[:] if "head_command" in f else None
            action, wheeled = recording_action_to_replay(cast(h5py.Dataset, f["action"])[:], head=head)
        if len(action) == 0:
            raise ValueError("Recorded episode has no timesteps.")

        skill_dir, in_place = self._resolve_replay_skill_dir(display_name, task_directory)
        skill_dir.mkdir(parents=True, exist_ok=True)
        self._write_h5_atomic(skill_dir / "episode_0.h5", action)
        self._write_json_atomic(
            skill_dir / "metadata.json",
            {
                "name": display_name,
                "type": "replay",
                "guidelines": guidelines,
                "guidelines_when_running": "",
                "inputs": {},
                "wheeled": wheeled,
                "execution": {
                    "model_type": "replay",
                    "replay_file": "episode_0.h5",
                    "replay_frequency": float(recorder_meta.get("data_frequency", 10.0)),
                    "start_pose": action[0, :6].tolist(),
                    "end_pose": action[-1, :6].tolist(),
                },
            },
        )
        self._drop_raw_recording(task_directory, skill_dir, in_place)

        skill_id = self._compute_skill_id(str(skill_dir))
        self._logger.info(f"Saved replay skill '{display_name}' at {skill_dir} (wheeled={wheeled})")
        self.reload_all()
        return str(skill_dir), skill_id, wheeled

    def _resolve_recording_episode(self, task_directory: str, episode_id: int) -> tuple[dict, Path]:
        """Load recorder metadata and resolve which episode_*.h5 to convert (episode_id < 0 = most recent)."""
        data_dir = Path(task_directory) / "data"
        meta_path = data_dir / "dataset_metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No recording metadata at {meta_path}; nothing to save.")
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"Recording metadata at {meta_path} is corrupt: {e}") from e

        episode_id = int(episode_id)
        if episode_id < 0:
            episode_id = int(meta.get("number_of_episodes", 0)) - 1
        if episode_id < 0:
            raise ValueError("No recorded episode to save.")

        file_by_id = {
            int(e["episode_id"]): e["file_name"]
            for e in meta.get("episodes", [])
            if e.get("episode_id") is not None and e.get("file_name")
        }
        episode_file = file_by_id.get(episode_id, f"episode_{episode_id}.h5")
        episode_path = data_dir / episode_file
        if not episode_path.exists():
            # The background dataset encoder moves a freshly-saved recording into
            # ../raw_data/ while it encodes MP4s / strips images, leaving a window
            # where the episode isn't under data/. Fall back to the raw copy (same
            # action data) — mirrors the recorder's own prefer-data/-fall-back-to-raw.
            raw_path = data_dir.parent / "raw_data" / episode_file
            if not raw_path.exists():
                raise FileNotFoundError(f"Recorded episode not found: {episode_path}")
            episode_path = raw_path
        return meta, episode_path

    def _resolve_replay_skill_dir(self, display_name: str, task_directory: str) -> tuple[Path, bool]:
        """Pick custom_skills/<slug> and enforce the overwrite policy:
        overwriting our own replay skill is fine; refuse a trained model or a
        different existing skill. Writes nothing."""
        dir_name = self._slugify(display_name)
        if not dir_name:
            raise ValueError(f"Cannot derive a directory name from '{display_name}'.")
        skill_dir = get_custom_skills_dir() / dir_name
        in_place = skill_dir.resolve() == Path(task_directory).resolve()  # name-first flow

        existing = skill_dir / "metadata.json"
        if existing.exists():
            meta = json.loads(existing.read_text())
            if meta.get("type") == "replay":
                self._logger.warning(f"Overwriting existing replay skill at {skill_dir}")
            elif self._has_trained_model(skill_dir, meta) or not in_place:
                raise ValueError(f"A non-replay skill named '{display_name}' already exists.")
            else:
                self._logger.info(f"Converting recording scratch into a replay skill at {skill_dir}")
        return skill_dir, in_place

    @staticmethod
    def _write_h5_atomic(path: Path, action) -> None:
        """Write the replay trajectory via tmp file + os.replace (never a truncated episode_0.h5)."""
        tmp_path = path.with_name(f"{path.name}.tmp")
        try:
            with h5py.File(tmp_path, "w") as f:
                f.create_dataset("action", data=action)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _drop_raw_recording(self, task_directory: str, skill_dir: Path, in_place: bool) -> None:
        """Delete the converted recording: data/ in place, else the whole scratch dir if it's a safe scratch."""
        if in_place:
            shutil.rmtree(skill_dir / "data", ignore_errors=True)
            return
        scratch = Path(task_directory).resolve()
        if self._is_recording_scratch(scratch):
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            self._logger.warning(f"Not deleting recording dir (not a safe scratch): {scratch}")

    def delete_skill(self, skill_directory: str) -> None:
        """Delete a user-created skill directory. Refuses anything outside custom_skills."""
        custom_root = get_custom_skills_dir().resolve()
        target = Path(skill_directory).resolve()
        if target == custom_root or not target.is_relative_to(custom_root):
            raise ValueError(f"Refusing to delete non-user skill: {target}")
        shutil.rmtree(target)
        self._logger.info(f"Deleted skill {target}")
        self.reload_all()

    @staticmethod
    def _has_trained_model(skill_dir: Path, meta: dict) -> bool:
        """True if skill_dir holds a trained learned-policy checkpoint we must not destroy."""
        checkpoint = (meta.get("execution") or {}).get("checkpoint")
        return bool(checkpoint) and (skill_dir / checkpoint).exists()

    def _is_recording_scratch(self, d: Path) -> bool:
        """True only for a throwaway recording dir under custom_skills: recorded episodes
        under data/, no trained model, and not a finished replay skill — so cleanup can
        never delete a real skill if a caller passes the wrong path."""
        custom_root = get_custom_skills_dir().resolve()
        if d == custom_root or not d.is_relative_to(custom_root) or not (d / "data").is_dir():
            return False
        meta_path = d / "metadata.json"
        if not meta_path.exists():
            return True
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        return meta.get("type") != "replay" and not self._has_trained_model(d, meta)

    # --- publishing ---
    def publish_skills_list(self) -> None:
        """Build and publish the full AvailableSkills message on the latched topic."""
        msg = AvailableSkills()
        skills = []
        drafts = []

        with self._skills_lock:
            code_skills_snapshot = dict(self._code_skills)
            physical_skills_snapshot = dict(self._physical_skills)
            in_training_skills_snapshot = dict(self._in_training_skills)
            broken_skills_snapshot = dict(self._broken_skills)

        for skill_id, entry in code_skills_snapshot.items():
            info = self._build_skill_info(
                skill_id=skill_id,
                name=entry.display_name,
                skill_type="code",
                group=entry.group,
                guidelines=entry.guidelines,
                guidelines_when_running=entry.guidelines_when_running,
                inputs_json=entry.inputs_json,
            )
            (drafts if entry.draft else skills).append(info)

        physical_infos = []
        physical_dirs_by_id = {}
        for snapshot in (physical_skills_snapshot, in_training_skills_snapshot):
            for skill_id, entry in snapshot.items():
                try:
                    info = self._build_physical_skill_info(skill_id, entry)
                except Exception as e:
                    self._logger.error(f"Skipping physical skill '{skill_id}' in available_skills: {e}")
                    continue
                skills.append(info)
                physical_infos.append(info)
                physical_dirs_by_id[skill_id] = entry.directory
        self._write_physical_refs(physical_infos, physical_dirs_by_id)

        # Broken skills ride the roster too — the UI shows them with their error
        # instead of them silently vanishing. Consumers that run or register
        # skills skip any entry with a non-empty load_error.
        for skill_id, error in broken_skills_snapshot.items():
            # A broken module in a subfolder rosters as "<ns>/chess.foo":
            # show it as "foo" grouped under "chess", like its healthy peers.
            dotted = skill_id.split("/", 1)[-1]
            group, _, leaf = dotted.rpartition(".")
            info = self._build_skill_info(
                skill_id=skill_id,
                name=leaf,
                skill_type="broken",
                group=group.replace(".", "/"),
                guidelines="",
                guidelines_when_running="",
                inputs_json="{}",
                load_error=error,
            )
            (drafts if _broken_draft(group, leaf) else skills).append(info)

        filtered_skills = self._dedupe_display_names(skills)

        msg.skills = filtered_skills
        try:
            self._skills_publisher.publish(msg)
            self._last_published_msg = msg
            self._write_skill_cache(filtered_skills, drafts)
            withheld = f" ({len(drafts)} draft on trial withheld)" if drafts else ""
            self._logger.info(f"Published {len(filtered_skills)} skills on /brain/available_skills{withheld}")
        except Exception as e:
            self._logger.error(f"Failed to publish AvailableSkills (had {len(filtered_skills)} entries): {e}")

    def _dedupe_display_names(self, skills: list[SkillInfo]) -> list[SkillInfo]:
        """Enforce unique display names (the LLM can't disambiguate them).
        A runnable skill outranks a broken one, then a ``local/`` skill claims
        the plain name; a same-name skill from any other namespace (shipped or
        a dropped-in package) stays published under ``name (<namespace>)`` so
        full-id references keep working. Same-namespace duplicates are a
        mistake: first wins."""

        def namespace(skill: SkillInfo) -> str:
            return skill.id.split("/", 1)[0]

        def plain_name_winner(first: SkillInfo, second: SkillInfo) -> tuple[SkillInfo, SkillInfo]:
            """(keeps the plain name, gets qualified). A broken skill never
            takes the plain name from a runnable one: an unimportable
            custom_skills/foo.py would otherwise rename the shipped foo the
            agent calls by name — and broken entries aren't registered with
            the cloud at all, so the plain name would resolve to nothing."""
            if bool(first.load_error) != bool(second.load_error):
                return (second, first) if first.load_error else (first, second)
            return (second, first) if namespace(second) == "local" else (first, second)

        deduped: list[SkillInfo] = []
        by_name: dict[str, SkillInfo] = {}
        for skill in skills:
            existing = by_name.get(skill.name)
            if existing is None:
                by_name[skill.name] = skill
                deduped.append(skill)
                continue
            if namespace(skill) == namespace(existing):
                self._logger.error(
                    f"DUPLICATE skill name '{skill.name}' between {existing.id} and {skill.id}. "
                    f"Skipping '{skill.id}' — rename the skill to fix this."
                )
                continue
            # Different namespaces: runnable beats broken, then local keeps the
            # plain name; between two equal-rank namespaces the first seen keeps
            # it (scan order: shipped before packages). The other is published
            # qualified, not dropped.
            keeps_plain, qualify = plain_name_winner(existing, skill)
            qualified = f"{qualify.name} ({namespace(qualify)})"
            if qualified in by_name:
                self._logger.error(
                    f"DUPLICATE skill name '{qualified}' between {by_name[qualified].id} and {qualify.id}. "
                    f"Skipping '{qualify.id}' — rename the skill to fix this."
                )
                continue
            self._logger.warning(
                f"Skill '{keeps_plain.name}': {keeps_plain.id} keeps the plain name; "
                f"publishing {qualify.id} as '{qualified}'"
            )
            qualify.name = qualified
            by_name[keeps_plain.name] = keeps_plain
            by_name[qualified] = qualify
            # whichever was seen first is already in deduped; append the newcomer
            deduped.append(skill)
        return deduped

    def _refresh_physical_refs(
        self, physical: dict[str, PhysicalSkillEntry], in_training: dict[str, PhysicalSkillEntry]
    ) -> None:
        """Regenerate the refs from freshly loaded entries — the pre-pass run
        before each code-skill import (see __init__). publish_skills_list
        writes them again from the roster; the content-compare in write_refs
        makes the second write a no-op."""
        infos = []
        dirs_by_id = {}
        for snapshot in (physical, in_training):
            for skill_id, entry in snapshot.items():
                try:
                    infos.append(self._build_physical_skill_info(skill_id, entry))
                    dirs_by_id[skill_id] = entry.directory
                except Exception as e:
                    self._logger.error(f"Skipping physical skill '{skill_id}' in physical refs: {e}")
        self._write_physical_refs(infos, dirs_by_id)

    def _write_physical_refs(self, physical_infos: list[SkillInfo], dirs_by_id: dict[str, str]) -> None:
        """Regenerate workspace/physical_skills/ — the typed refs agents and
        skills import instead of id strings — plus the per-recording-folder
        ``__init__.py`` shims that make the pack-local import spelling work.
        Content-compared inside, so an unchanged roster writes nothing (and
        can't loop the file watcher)."""
        entries = [
            {
                "id": info.id,
                "guidelines": info.guidelines,
                "type": info.type,
                "episode_count": info.episode_count,
                "in_training": info.in_training,
                "dir": dirs_by_id.get(info.id),
            }
            for info in physical_infos
        ]
        write_refs(get_workspace_dir() / "physical_skills", render_refs(entries), self._logger)
        shims = render_dir_shims(entries)
        write_dir_shims(shims, self._logger)
        prune_dir_shims(self._skills_directories, shims, self._logger)

    def republish_cached(self) -> None:
        """Re-emit the last published roster, no rebuild — the heartbeat for
        late subscribers (see _last_published_msg). Consumers dedupe by
        content, so repeats are a no-op for the UI."""
        if self._last_published_msg is not None:
            self._skills_publisher.publish(self._last_published_msg)

    def _build_skill_info(
        self,
        skill_id: str,
        name: str,
        skill_type: str,
        guidelines: str,
        guidelines_when_running: str,
        inputs_json: str,
        group: str = "",
        in_training: bool = False,
        episode_count: int = 0,
        directory: str = "",
        wheeled: bool = False,
        load_error: str = "",
    ) -> SkillInfo:
        msg = SkillInfo()
        msg.id = skill_id or ""
        msg.name = name or ""
        msg.type = skill_type or ""
        msg.group = group or ""
        msg.guidelines = guidelines or ""
        msg.guidelines_when_running = guidelines_when_running or ""
        msg.inputs_json = inputs_json or ""
        msg.in_training = bool(in_training)
        msg.episode_count = int(episode_count or 0)
        msg.directory = directory or ""
        msg.wheeled = bool(wheeled)
        msg.load_error = load_error or ""
        return msg

    def _inspect_skill_inputs(self, skill_id: str, skill_instance) -> tuple[dict, frozenset[str], bool]:
        """Best-effort introspection of a code skill's execute() signature.

        Returns ``(inputs schema, float-annotated params, accepts **kwargs)``.
        The permissive fallback for an un-introspectable signature is
        ``({}, frozenset(), True)``: nothing coerces, and validation lets the
        call itself decide.
        """
        if not hasattr(skill_instance, "execute"):
            return {}, frozenset(), True
        try:
            signature = inspect.signature(skill_instance.execute)
        except (TypeError, ValueError) as e:
            self._logger.warn(f"Could not inspect execute() signature for '{skill_id}': {e}")
            return {}, frozenset(), True

        inputs: dict = {}
        float_params: set[str] = set()
        accepts_extra = False
        for param_name, param in signature.parameters.items():
            if param_name == "self":
                continue
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                accepts_extra = True
                continue  # *args/**kwargs are compat plumbing, not inputs
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                continue
            if _annotation_is_float(param.annotation):
                float_params.add(param_name)
            param_type = "any"
            enum_values = None
            try:
                if param.annotation != inspect.Parameter.empty:
                    origin = get_origin(param.annotation)
                    if origin is Literal:
                        values = list(get_args(param.annotation))
                        value_type_names = {"None" if value is None else type(value).__name__ for value in values}
                        param_type = value_type_names.pop() if len(value_type_names) == 1 else "literal"
                        enum_values = values
                    elif (
                        isinstance(param.annotation, (types.UnionType, types.GenericAlias))
                        or hasattr(param.annotation, "_name")
                        and param.annotation._name in ["List", "Optional", "Dict", "Tuple", "Union"]
                    ):
                        param_type = str(param.annotation)
                    elif hasattr(param.annotation, "__name__"):
                        param_type = param.annotation.__name__
                    else:
                        param_type = str(param.annotation)
                    if isinstance(param_type, str):
                        param_type = param_type.replace("typing.", "")
            except Exception as e:
                self._logger.warn(f"Could not stringify annotation for '{skill_id}.{param_name}': {e}")
                param_type = "any"

            param_schema = {"type": param_type, "required": param.default == inspect.Parameter.empty}
            if enum_values is not None:
                param_schema["enum"] = enum_values
            if param.default != inspect.Parameter.empty:
                try:
                    json.dumps(param.default)
                    param_schema["default"] = param.default
                except (TypeError, ValueError):
                    pass
            inputs[param_name] = param_schema
        return inputs, frozenset(float_params), accepts_extra

    def _safe_skill_string(self, skill_id: str, skill_instance, attr: str) -> str:
        if not hasattr(skill_instance, attr):
            return ""
        try:
            value = getattr(skill_instance, attr)()
        except Exception as e:
            self._logger.warn(f"Skill '{skill_id}'.{attr}() raised: {e}; defaulting to empty string")
            return ""
        return str(value) if value is not None else ""

    def _build_physical_skill_info(self, skill_id: str, entry: PhysicalSkillEntry) -> SkillInfo:
        metadata = entry.metadata
        try:
            inputs_json = json.dumps(metadata.get("inputs", {}))
        except (TypeError, ValueError) as e:
            self._logger.error(f"Could not serialize inputs for physical skill '{skill_id}': {e}; using empty inputs")
            inputs_json = "{}"

        return self._build_skill_info(
            skill_id=skill_id,
            name=metadata.get("name", skill_id),
            skill_type=metadata.get("type", "physical"),
            guidelines=metadata.get("guidelines", ""),
            guidelines_when_running=metadata.get("guidelines_when_running", ""),
            inputs_json=inputs_json,
            in_training=entry.in_training,
            # re-read at publish time, not entry.episode_count: episodes
            # accumulate while a skill trains, and on a robot without the
            # file watcher a count frozen at load would never move
            episode_count=get_episode_count(entry.directory, self._logger),
            directory=entry.directory,
            wheeled=bool(metadata.get("wheeled", False)),
        )

    # --- cache ---
    def _skill_cache_path(self) -> Path:
        return Path(os.environ.get("INNATE_SKILL_CACHE", "/tmp/innate_skill_contracts.json"))

    def _skill_info_to_cache_dict(self, skill: SkillInfo, *, draft: bool = False) -> dict:
        try:
            inputs = json.loads(skill.inputs_json or "{}")
        except json.JSONDecodeError:
            inputs = {}
        if not isinstance(inputs, dict):
            inputs = {}
        return {
            "id": skill.id,
            "name": skill.name,
            "type": skill.type,
            "group": skill.group,
            "inputs": inputs,
            "guidelines": skill.guidelines,
            "guidelines_when_running": skill.guidelines_when_running,
            "in_training": skill.in_training,
            "episode_count": skill.episode_count,
            "directory": skill.directory,
            "wheeled": skill.wheeled,
            "load_error": skill.load_error,
            "draft": draft,
        }

    def _write_skill_cache(self, skills: list[SkillInfo], drafts: list[SkillInfo]) -> None:
        """The cache carries drafts too, flagged: learn_skill watches it to see its file load."""
        cache_path = self._skill_cache_path()
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "skills": [self._skill_info_to_cache_dict(skill) for skill in skills]
            + [self._skill_info_to_cache_dict(skill, draft=True) for skill in drafts],
        }
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
            tmp_path.replace(cache_path)
        except OSError as exc:
            self._logger.warning(f"Failed to write skill cache {cache_path}: {exc}")


def _broken_draft(group: str, leaf: str) -> bool:
    """A learned module that failed to import while learn_skill is trying it: the file, not the roster, says so."""
    if group != LEARNED_GROUP:
        return False
    try:
        return is_draft(get_learned_skills_dir() / f"{leaf}.py")
    except OSError:  # the trial ended and the draft is already deleted
        return False
