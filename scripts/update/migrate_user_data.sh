#!/bin/bash
# User-data migration into the post-refactor layout.
#
# Source this file to get run_user_data_migrations(), or execute it directly:
#     bash migrate_user_data.sh <repo_dir>
#
# It relocates user-created content that older releases stored at the repo root
# (or, for primitives, under primitives/) into the layout the current code
# scans. It is idempotent, never overwrites an existing destination, and never
# deletes user data — on a name clash it leaves the source in place for manual
# reconciliation. Empty source dirs are removed with rmdir (empty-only).
#
#   <repo>/agents/*                       -> <repo>/workspace/custom_agents/
#   <repo>/directives/*                   -> <repo>/workspace/custom_agents/   (pre-0.2.9 layout)
#   <repo>/skills/*                        -> <repo>/workspace/custom_skills/
#   <repo>/primitives/**/*.{h5,pt,pth}     -> <repo>/workspace/innate_skills/<rel>
#   <repo>/inputs/*                        -> <repo>/workspace/inputs/
#   ~/agents/*                             -> <repo>/workspace/custom_agents/
#   ~/skills/*                             -> <repo>/workspace/custom_skills/
#   <repo>/maps/*                          -> <repo>/data/maps/
#   <repo>/.last_mode                      -> <repo>/data/.last_mode
#   <repo>/.last_map                       -> <repo>/data/.last_map
#   settings.yaml extra_skill_dirs entries -> <repo>/workspace/custom_skills/<name> (symlink)
#   settings.yaml extra_agent_dirs entries -> <repo>/workspace/custom_agents/*     (symlinks)
#
# The home-dir lanes (~/skills, ~/agents) were scanned in place through 0.6.x;
# 0.7 loads only from workspace/, so they are migrated here rather than left to
# stop loading silently. custom_skills/custom_agents keeps their ids unchanged
# (local/<name>).
#
# The extra-dirs lanes (config/settings.yaml script_paths.extra_skill_dirs /
# extra_agent_dirs, shipped 0.6.0 through 0.7.0-rc1) pointed the loaders at
# directories anywhere on the machine; the setting is gone in 0.7. Configured
# dirs are symlinked into workspace/ so their content keeps loading — never
# moved, since an extra dir may be shared state (e.g. /opt/team/skills).
# A skill dir becomes a custom_skills/ *subpackage* rather than a
# workspace-root pack: that keeps the local/<name> skill ids its skills had
# (agents and anything persisted reference those), where a root pack would
# re-namespace them <dirname>/<name>. Agent dirs have no pack equivalent
# (agents load from custom_agents/ top-level files only), so their top-level
# entries are linked into custom_agents/ one by one — non-Python assets too,
# because display icons resolve relative to the agent file's directory.
#
# Optional env: MIGRATE_CHOWN_USER — when set and running as root, moved files
# are chowned to this user (best-effort). Unset (e.g. in CI) => no chown.
# Optional env: MIGRATE_HOME — home dir to migrate the ~/ lanes from; defaults
# to $HOME. post_update.sh passes ACTUAL_HOME so sudo doesn't scan /root.

# Log via the host script's log() *function* if defined (post_update.sh), else
# stdout. Uses `declare -F` so we don't accidentally match an external `log`
# binary (e.g. macOS /usr/bin/log).
_mig_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$@"
    else
        echo "[migrate] $*"
    fi
}

_mig_chown() {
    local path="$1"
    if [ -n "${MIGRATE_CHOWN_USER:-}" ] && [ "$(id -u)" -eq 0 ]; then
        chown -R "$MIGRATE_CHOWN_USER:$MIGRATE_CHOWN_USER" "$path" 2>/dev/null || true
    fi
}

# Like `mkdir -p`, but chowns every directory level it creates to
# MIGRATE_CHOWN_USER (when root). Plain `mkdir -p` run as root would leave the
# intermediate dirs (e.g. workspace/innate_skills/<skill>/, data/) root-owned,
# locking the robot user out. Existing dirs are left untouched. Builds parents
# first so each chown -R only sees the empty dir it just created.
_mig_mkdir() {
    local dir="$1"
    if [ -d "$dir" ]; then
        return 0
    fi
    _mig_mkdir "$(dirname "$dir")"
    mkdir "$dir"
    _mig_chown "$dir"
}

# Move a single entry, skipping (never clobbering) an existing destination.
_mig_move() {
    local src="$1" dst="$2" label="$3"
    if [ -e "$dst" ]; then
        _mig_log "  Kept $label (already exists at destination — reconcile manually)"
    else
        _mig_mkdir "$(dirname "$dst")"
        mv "$src" "$dst"
        _mig_chown "$dst"
        _mig_log "  Moved $label"
    fi
}

# Move every child of $old_path into $new_path. $old_label is what the logs
# call the source ("skills/", "~/skills/").
_migrate_path_into_workspace() {
    local old_path="$1" new_path="$2" old_label="$3" new_rel="$4"
    [ -d "$old_path" ] || return 0

    shopt -s dotglob nullglob
    local items=("$old_path"/*)
    shopt -u dotglob nullglob

    if [ ${#items[@]} -gt 0 ]; then
        _mig_log "Migrating $old_label/ -> workspace/$new_rel/"
        _mig_mkdir "$new_path"
        local item name
        for item in "${items[@]}"; do
            name=$(basename "$item")
            if [ "$name" = "__pycache__" ]; then
                rm -rf "$item"
                continue
            fi
            _mig_move "$item" "$new_path/$name" "$old_label/$name -> workspace/$new_rel/$name"
        done
    fi

    rmdir "$old_path" 2>/dev/null && _mig_log "Removed empty $old_label/" || true
}

# Move every child of <repo>/$old_rel into <repo>/workspace/$new_rel.
_migrate_dir_into_workspace() {
    local repo="$1" old_rel="$2" new_rel="$3"
    _migrate_path_into_workspace "$repo/$old_rel" "$repo/workspace/$new_rel" "$old_rel" "$new_rel"
}

# Same, for the home-dir lanes (~/skills, ~/agents). These were scanned in
# place through 0.6.x and are not scanned at all as of 0.7, so without this
# their content would silently stop loading. MIGRATE_HOME is the *invoking
# user's* home (post_update.sh passes ACTUAL_HOME): under sudo, $HOME is
# /root and the real content would be missed.
_migrate_home_dir_into_workspace() {
    local repo="$1" home_rel="$2" new_rel="$3"
    local home="${MIGRATE_HOME:-${HOME:-}}"
    [ -n "$home" ] || return 0
    # INNATE_OS_ROOT=~ collapses ~/skills onto <repo>/skills, already migrated
    # above by the repo-relative pass; skip so it isn't reported twice.
    [ "$home" != "$repo" ] || return 0
    _migrate_path_into_workspace "$home/$home_rel" "$repo/workspace/$new_rel" "~/$home_rel" "$new_rel"
}

# Move trained-model files out of the legacy primitives/ tree into innate_skills,
# preserving the relative sub-path (e.g. primitives/wave/x.h5 -> innate_skills/wave/x.h5).
_migrate_primitives_models() {
    local repo="$1"
    local prim="$repo/primitives"
    local dest="$repo/workspace/innate_skills"
    [ -d "$prim" ] || return 0

    _mig_log "Migrating primitives model files -> workspace/innate_skills/"
    local f rel
    while IFS= read -r f; do
        rel="${f#"$prim"/}"
        _mig_move "$f" "$dest/$rel" "primitives/$rel -> innate_skills/$rel"
    done < <(find "$prim" -type f \( -name "*.h5" -o -name "*.pt" -o -name "*.pth" \))

    find "$prim" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    # Remove now-empty dirs bottom-up. rmdir only deletes empty dirs (never
    # files), matching the empty-only contract; any dir with leftover files stays.
    find "$prim" -depth -type d -exec rmdir {} \; 2>/dev/null || true
    if [ -d "$prim" ]; then
        _mig_log "Kept primitives/ (still contains files after migration)"
    else
        _mig_log "Removed empty primitives/"
    fi
}

# Move SLAM maps and nav-state files into data/.
_migrate_nav_state() {
    local repo="$1"
    local data="$repo/data"
    local old_maps="$repo/maps"

    if [ -d "$old_maps" ]; then
        shopt -s dotglob nullglob
        local items=("$old_maps"/*)
        shopt -u dotglob nullglob
        if [ ${#items[@]} -gt 0 ]; then
            _mig_log "Migrating maps/ -> data/maps/"
            local item name
            for item in "${items[@]}"; do
                name=$(basename "$item")
                # The repo-root .gitignore marker is a shipped file, not user data.
                [ "$name" = ".gitignore" ] && continue
                _mig_mkdir "$data/maps"
                _mig_move "$item" "$data/maps/$name" "maps/$name -> data/maps/$name"
            done
        fi
        rmdir "$old_maps" 2>/dev/null && _mig_log "Removed empty maps/" || true
    fi

    local f
    for f in .last_mode .last_map; do
        if [ -f "$repo/$f" ]; then
            _mig_mkdir "$data"
            _mig_move "$repo/$f" "$data/$f" "$f -> data/$f"
        fi
    done
}

# Symlink $src to $dst unless $dst exists. A symlink already pointing at $src
# is the previous run's work — silent no-op, keeping re-runs idempotent.
_mig_symlink() {
    local src="$1" dst="$2" label="$3"
    if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
        return 0
    fi
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        _mig_log "  Kept $label (already exists at destination — reconcile manually)"
        return 0
    fi
    _mig_mkdir "$(dirname "$dst")"
    ln -s "$src" "$dst"
    if [ -n "${MIGRATE_CHOWN_USER:-}" ] && [ "$(id -u)" -eq 0 ]; then
        chown -h "$MIGRATE_CHOWN_USER:$MIGRATE_CHOWN_USER" "$dst" 2>/dev/null || true
    fi
    _mig_log "  Linked $label"
}

# True if $1 has any visible entry besides __pycache__ (i.e. is worth linking).
_dir_has_content() {
    [ -n "$(find "$1" -mindepth 1 -maxdepth 1 ! -name '.*' ! -name '__pycache__' -print -quit 2>/dev/null)" ]
}

# Print "<kind>\t<abs-dir>\t<pack-name>" per configured extra dir, kind in
# {skill, agent}. Needs python3 + PyYAML (both ship with the robot's ROS
# stack): exit 3 = no PyYAML, 4 = settings.yaml unparseable. ``~`` expands
# against MIGRATE_HOME, not $HOME — under sudo, $HOME is /root. Pack names
# must be Python identifiers (a custom_skills subpackage is imported); the
# old exec-based scanner had no such constraint, so sanitize.
_extra_dirs_plan() {
    MIGRATE_HOME="${MIGRATE_HOME:-${HOME:-}}" python3 - "$1" <<'PY'
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit(3)

home = os.environ.get("MIGRATE_HOME") or os.path.expanduser("~")
try:
    with open(sys.argv[1]) as f:
        data = yaml.safe_load(f) or {}
except Exception:
    sys.exit(4)

section = data.get("script_paths")
params = section.get("ros__parameters") if isinstance(section, dict) else None
params = params if isinstance(params, dict) else {}


def dirs(key):
    # Same shapes load_extra_script_dirs() accepted: YAML list or
    # os.pathsep-joined string, blanks dropped, ~ and $VARS expanded.
    value = params.get(key)
    if isinstance(value, str):
        parts = value.split(os.pathsep)
    elif isinstance(value, list):
        parts = [str(p) for p in value]
    else:
        parts = []
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part == "~" or part.startswith("~/"):
            part = home + part[1:]
        part = os.path.abspath(os.path.expandvars(os.path.expanduser(part)))
        if part not in out:
            out.append(part)
    return out


def pack_name(src):
    name = re.sub(r"[^0-9A-Za-z_]", "_", os.path.basename(src.rstrip("/")))
    if not name or name[0].isdigit() or name.startswith("_"):
        # _-prefixed packages are skipped by discovery; digits can't start one.
        name = "pack_" + name.lstrip("_")
    return name


for src in dirs("extra_skill_dirs"):
    print(f"skill\t{src}\t{pack_name(src)}")
for src in dirs("extra_agent_dirs"):
    print(f"agent\t{src}\t-")
PY
}

# Symlink the dirs configured in settings.yaml script_paths (see header) into
# workspace/. Must run AFTER the move passes: an extra dir pointing at a moved
# lane (~/skills, <repo>/skills) is empty or gone by the time this runs, and
# is skipped here rather than left as a hollow link.
_migrate_extra_script_dirs() {
    local repo="$1"
    local settings="$repo/config/settings.yaml"
    [ -f "$settings" ] || return 0
    # Cheap pre-filter: nothing to do (and no parser needed) unless the
    # setting appears outside a comment.
    grep -Eq '^[^#]*extra_(skill|agent)_dirs' "$settings" || return 0

    local plan
    if ! plan=$(_extra_dirs_plan "$settings"); then
        _mig_log "WARNING: settings.yaml sets script_paths.extra_*_dirs but the section could not be parsed (python3/PyYAML missing, or malformed YAML)."
        _mig_log "WARNING: 0.7 no longer scans extra dirs — symlink them into workspace/ by hand (see config/settings.yaml.template)."
        return 0
    fi
    [ -n "$plan" ] || return 0

    local home="${MIGRATE_HOME:-${HOME:-}}"
    # Canonicalize the paths compared against below, so a symlinked repo or
    # home still matches. _canon leaves a non-existent path as-is.
    _canon() { readlink -f "$1" 2>/dev/null || echo "$1"; }
    local ws_resolved repo_resolved home_resolved
    ws_resolved=$(_canon "$repo/workspace")
    repo_resolved=$(_canon "$repo")
    home_resolved=$(_canon "${home:-//nonexistent}")

    _mig_log "Migrating settings.yaml extra script dirs -> workspace/ symlinks"
    local kind src name resolved entry base
    while IFS=$'\t' read -r kind src name; do
        [ -n "$kind" ] || continue
        if [ ! -d "$src" ]; then
            _mig_log "  Skipped $kind dir $src (not a directory — moved by a pass above, or never existed)"
            continue
        fi
        resolved=$(_canon "$src")
        case "$resolved" in
            "$ws_resolved" | "$ws_resolved"/*)
                _mig_log "  Skipped $kind dir $src (already inside workspace/ — loads without migration)"
                continue
                ;;
        esac
        # Lanes the move passes above own; their contents already moved (any
        # leftovers are name clashes those passes logged for manual review).
        case "$resolved" in
            "$repo_resolved"/skills | "$repo_resolved"/agents | "$repo_resolved"/directives | \
                "$home_resolved"/skills | "$home_resolved"/agents)
                _mig_log "  Skipped $kind dir $src (contents moved by the migration above)"
                continue
                ;;
        esac
        if ! _dir_has_content "$src"; then
            _mig_log "  Skipped $kind dir $src (empty)"
            continue
        fi
        if [ "$kind" = "skill" ]; then
            _mig_symlink "$src" "$repo/workspace/custom_skills/$name" \
                "extra skill dir $src -> workspace/custom_skills/$name"
        else
            # Per-entry links: agents load from custom_agents/ itself, and a
            # linked subdirectory would not be scanned.
            for entry in "$src"/*; do
                [ -e "$entry" ] || continue
                base=$(basename "$entry")
                [ "$base" = "__pycache__" ] && continue
                _mig_symlink "$entry" "$repo/workspace/custom_agents/$base" \
                    "extra agent entry $src/$base -> workspace/custom_agents/$base"
            done
        fi
    done <<< "$plan"
}

# The 0.8 STT rework removed the OpenAI backend and the stt_realtime_* knobs.
# Undeclared ROS params load with no warning at all, so a settings.yaml still
# carrying them would silently lose its tuning — comment them out loudly here.
# The brain's model became provider-neutral: gemini_model → llm_model
# ("google:" + the old value), gemini_thinking_level → llm_thinking.
_migrate_llm_settings() {
    local settings="$1/config/settings.yaml"
    [ -f "$settings" ] || return 0
    if grep -qE '^\s*gemini_model:' "$settings"; then
        sed -i -E 's|^(\s*)gemini_model:\s*"?([^"#[:space:]]+)"?\s*(#.*)?$|\1llm_model: "google:\2"  # was gemini_model|' "$settings"
        _mig_log "settings.yaml: gemini_model renamed to llm_model (google:<model>)."
    fi
    if grep -qE '^\s*gemini_thinking_level:' "$settings"; then
        sed -i -E 's|^(\s*)gemini_thinking_level:|\1llm_thinking:|' "$settings"
        _mig_log "settings.yaml: gemini_thinking_level renamed to llm_thinking."
    fi
}

_migrate_stt_settings() {
    local settings="$1/config/settings.yaml"
    [ -f "$settings" ] || return 0
    if grep -qE '^\s*stt_backend:\s*"?openai"?\s*(#.*)?$' "$settings"; then
        sed -i -E 's|^(\s*)stt_backend:\s*"?openai"?\s*(#.*)?$|\1stt_backend: "elevenlabs"  # was "openai" — backend removed in 0.8, migrated to Scribe realtime|' "$settings"
        _mig_log "WARNING: settings.yaml pinned stt_backend: openai — that backend was removed; switched to 'elevenlabs' (Scribe realtime)."
    fi
    local key
    for key in stt_realtime_vad_threshold stt_realtime_vad_silence_secs \
        openai_realtime_model openai_realtime_url openai_transcribe_model; do
        if grep -qE "^\s*${key}:" "$settings"; then
            sed -i -E "s|^(\s*)(${key}:.*)$|\1# \2  # removed in 0.8 (vendor VAD retired; local endpointing uses stt_vad_*)|" "$settings"
            _mig_log "WARNING: settings.yaml sets ${key}, which no longer exists — commented out (local endpointing is tuned by stt_vad_silence_secs / stt_vad_threshold)."
        fi
    done
}

run_user_data_migrations() {
    local repo="${1:?run_user_data_migrations: repo dir required}"
    _migrate_dir_into_workspace "$repo" agents     custom_agents
    _migrate_dir_into_workspace "$repo" directives custom_agents
    _migrate_dir_into_workspace "$repo" skills     custom_skills
    _migrate_dir_into_workspace "$repo" inputs     inputs
    _migrate_home_dir_into_workspace "$repo" agents custom_agents
    _migrate_home_dir_into_workspace "$repo" skills custom_skills
    _migrate_extra_script_dirs  "$repo"
    _migrate_primitives_models  "$repo"
    _migrate_nav_state          "$repo"
    _migrate_stt_settings       "$repo"
    _migrate_llm_settings       "$repo"
}

# Execute when run directly (not when sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    set -euo pipefail
    run_user_data_migrations "${1:?usage: migrate_user_data.sh <repo_dir>}"
fi
