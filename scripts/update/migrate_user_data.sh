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
#   <repo>/maps/*                          -> <repo>/data/maps/
#   <repo>/.last_mode                      -> <repo>/data/.last_mode
#   <repo>/.last_map                       -> <repo>/data/.last_map
#   ~/agents/*                             -> <repo>/workspace/custom_agents/
#   ~/skills/*                             -> <repo>/workspace/custom_skills/
#
# The home-directory skills/agents (~/skills, ~/agents) are folded in here too.
# The brain still scans them in place for backwards compatibility, but the
# dataset media server and encoder only serve workspace/custom_skills, so a
# ~/skills dataset would otherwise list but be unplayable (no thumbnails/video/
# joints). Moving them into the workspace keeps both pipelines consistent.
#
# Optional env: MIGRATE_CHOWN_USER — when set and running as root, moved files
# are chowned to this user (best-effort). Unset (e.g. in CI) => no chown.

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

# Move every child of <repo>/$old_rel into <repo>/workspace/$new_rel.
_migrate_dir_into_workspace() {
    local repo="$1" old_rel="$2" new_rel="$3"
    local old_path="$repo/$old_rel"
    local new_path="$repo/workspace/$new_rel"
    [ -d "$old_path" ] || return 0

    shopt -s dotglob nullglob
    local items=("$old_path"/*)
    shopt -u dotglob nullglob

    if [ ${#items[@]} -gt 0 ]; then
        _mig_log "Migrating $old_rel/ -> workspace/$new_rel/"
        _mig_mkdir "$new_path"
        local item name
        for item in "${items[@]}"; do
            name=$(basename "$item")
            if [ "$name" = "__pycache__" ]; then
                rm -rf "$item"
                continue
            fi
            _mig_move "$item" "$new_path/$name" "$old_rel/$name -> workspace/$new_rel/$name"
        done
    fi

    rmdir "$old_path" 2>/dev/null && _mig_log "Removed empty $old_rel/" || true
}

# The robot user's home directory — not root's. post_update.sh runs the
# migration as root, but the legacy ~/skills and ~/agents belong to the login
# user (MIGRATE_CHOWN_USER), so resolve their home rather than using $HOME.
_mig_home_dir() {
    local user="${MIGRATE_CHOWN_USER:-}"
    local home=""
    if [ -n "$user" ]; then
        home=$(getent passwd "$user" 2>/dev/null | cut -d: -f6)
    fi
    [ -n "$home" ] || home="$HOME"
    echo "$home"
}

# Move every child of an absolute legacy home dir (~/skills, ~/agents) into
# <repo>/workspace/$new_rel. Same never-clobber/empty-only contract as
# _migrate_dir_into_workspace; the source just lives outside the repo.
_migrate_home_dir_into_workspace() {
    local repo="$1" home_src="$2" new_rel="$3"
    local new_path="$repo/workspace/$new_rel"
    [ -d "$home_src" ] || return 0
    # Never touch a ~/skills that is actually a symlink into the workspace.
    [ -L "$home_src" ] && return 0

    shopt -s dotglob nullglob
    local items=("$home_src"/*)
    shopt -u dotglob nullglob

    if [ ${#items[@]} -gt 0 ]; then
        _mig_log "Migrating $home_src/ -> workspace/$new_rel/"
        _mig_mkdir "$new_path"
        local item name
        for item in "${items[@]}"; do
            name=$(basename "$item")
            if [ "$name" = "__pycache__" ]; then
                rm -rf "$item"
                continue
            fi
            _mig_move "$item" "$new_path/$name" "$home_src/$name -> workspace/$new_rel/$name"
        done
    fi

    rmdir "$home_src" 2>/dev/null && _mig_log "Removed empty $home_src/" || true
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

run_user_data_migrations() {
    local repo="${1:?run_user_data_migrations: repo dir required}"
    _migrate_dir_into_workspace "$repo" agents     custom_agents
    _migrate_dir_into_workspace "$repo" directives custom_agents
    _migrate_dir_into_workspace "$repo" skills     custom_skills
    _migrate_dir_into_workspace "$repo" inputs     inputs
    _migrate_primitives_models  "$repo"
    _migrate_nav_state          "$repo"

    # Legacy home-directory locations (~/skills, ~/agents) the brain still scans
    # in place but the dataset media/encoder pipeline can't serve.
    local home_dir
    home_dir="$(_mig_home_dir)"
    if [ -n "$home_dir" ]; then
        _migrate_home_dir_into_workspace "$repo" "$home_dir/agents" custom_agents
        _migrate_home_dir_into_workspace "$repo" "$home_dir/skills" custom_skills
    fi
}

# Execute when run directly (not when sourced).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    set -euo pipefail
    run_user_data_migrations "${1:?usage: migrate_user_data.sh <repo_dir>}"
fi
