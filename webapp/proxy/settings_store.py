# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Read and write ``config/settings.yaml`` for the webapp Settings page.

``settings.yaml`` is a ROS 2 params overlay built from ``settings.yaml.template``:
every tunable is a documented, commented stanza you uncomment to activate.

We keep the file **standardised**: it is always the template with the *active*
overrides uncommented + set to their values, regenerated deterministically on
every save. That means no cruft can accumulate, the documented menu of every
knob stays in the file, and the output is identical for the same set of
overrides regardless of edit history.

The trick that makes this robust (an earlier in-place surgical version drifted):

* **structure** comes from the *pristine template* — consistently indented, so
  parsing it for each knob's location is reliable;
* **values** come from the live file via PyYAML (the active overrides);
* on save we apply the page's changes to that override set and **regenerate** the
  whole file from the template. The messy live file is never parsed for structure.

Overrides for knobs not in the template (hand-added) are kept in an "Extra
overrides" block at the end. Hand-edited *values* survive (PyYAML reads them);
hand-written prose comments don't (the file is regenerated from the template).
"""

import os
import tempfile
import threading
from pathlib import Path

import yaml

# Serialises the read-modify-write in apply_changes so two concurrent saves
# (e.g. two browser tabs) can't clobber each other.
_WRITE_LOCK = threading.Lock()


def settings_path() -> Path:
    root = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
    return Path(root) / "config" / "settings.yaml"


def template_path() -> Path:
    return settings_path().with_name("settings.yaml.template")


def read_overrides() -> dict:
    """Active overrides as a nested dict (PyYAML on the file). {} if missing/empty."""
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    _carry_retired(data)
    return data


# Retired parameter path -> its current name, mirroring brain_client's
# core/config.py:_RENAMED_PARAMS (the webapp proxy cannot import it).
_RETIRED_PARAMS = {
    ("brain_client_node", "ros__parameters", "gemini_model"): "llm_model",
    ("brain_client_node", "ros__parameters", "gemini_thinking_level"): "llm_thinking",
}


def _carry_retired(overrides: dict) -> None:
    """Rename a deployed robot's retired keys in place, before anything reads them.

    A retired key no longer matches a template stanza, so leaving it would send it
    to the Extra block as a second `brain_client_node:` mapping — and PyYAML keeps
    only the last duplicate, dropping every other brain override on the next read.
    """
    for path, current in _RETIRED_PARAMS.items():
        *parents, retired = path
        node = overrides
        for key in parents:
            node = node.get(key) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
        if not isinstance(node, dict) or retired not in node:
            continue
        carried = node.pop(retired)
        node.setdefault(current, carried)


# ── override-dict edits ────────────────────────────────────────────────
def _coerce(value, typ: str):
    if typ == "float":
        return float(value)
    if typ == "int":
        return int(value)
    if typ == "bool":
        return bool(value)
    return value


def _set_path(d: dict, path, value) -> None:
    cur = d
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[path[-1]] = value


def _remove_path(d: dict, path) -> None:
    cur = d
    for k in path[:-1]:
        if not isinstance(cur, dict) or not isinstance(cur.get(k), dict):
            return
        cur = cur[k]
    if isinstance(cur, dict):
        cur.pop(path[-1], None)


def _prune_empty(d: dict) -> None:
    for k in list(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            _prune_empty(v)
            if not v:
                del d[k]
        elif v is None:
            del d[k]


def _flatten(d: dict, prefix=()):
    out = []
    for k, v in d.items():
        p = prefix + (k,)
        if isinstance(v, dict):
            out.extend(_flatten(v, p))
        else:
            out.append((p, v))
    return out


# ── template parse (comment-aware; the template is consistently indented) ──
class _Node:
    __slots__ = ("key", "level", "line", "parent", "children")

    def __init__(self, key, level, line, parent):
        self.key, self.level, self.line, self.parent = key, level, line, parent
        self.children = []


def _parse_key_line(raw: str):
    """If *raw* is a YAML ``key:`` line (active or ``# ``-commented) return
    ``(level, key)``; else None (blank / prose / section divider)."""
    line = raw
    if line.startswith("# "):
        line = line[2:]
    elif line.startswith("#"):
        return None
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    if indent % 2 != 0 or ":" not in stripped:
        return None
    key = stripped.split(":", 1)[0]
    if not key or any(c in key for c in " \t#'\"[]{}"):
        return None
    return indent // 2, key


def _build_tree(lines):
    root = _Node(None, -1, -1, None)
    stack = [root]
    for i, raw in enumerate(lines):
        parsed = _parse_key_line(raw)
        if parsed is None:
            continue
        level, key = parsed
        while stack[-1].level >= level:
            stack.pop()
        node = _Node(key, level, i, stack[-1])
        stack[-1].children.append(node)
        stack.append(node)
    return root


def _uncomment_chain(node, uncomment) -> None:
    """Activate a leaf and every ancestor key above it."""
    while node is not None and node.key is not None:
        uncomment.add(node.line)
        node = node.parent


def _find(root, path):
    node = root
    for key in path:
        node = next((c for c in node.children if c.key == key), None)
        if node is None:
            return None
    return node


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)  # always keeps a decimal point (ROS wants floats float)
    if isinstance(value, int):
        return str(value)
    dumped = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=10**9).strip()
    return dumped.split("\n", 1)[0].rstrip()


def _replace_value(line: str, value) -> str:
    """Set the value on an (uncommented) ``key: value  # doc`` line, keeping the
    indentation and trailing doc comment."""
    head, _sep, rest = line.partition(":")
    val_part, _hash, comment = rest.partition("#")
    body = f"{head}: {_fmt(value)}"
    return f"{body}  #{comment}" if _hash else body


def _regenerate(overrides: dict) -> str:
    """Render the template with *overrides* uncommented + valued; append any
    overrides not present in the template as an Extra block."""
    tpl = template_path()
    if not tpl.is_file():
        # No template to standardise against — fall back to a clean dump.
        return yaml.safe_dump(overrides, default_flow_style=False, sort_keys=False) if overrides else ""

    lines = tpl.read_text().split("\n")
    tree = _build_tree(lines)
    uncomment = set()
    value_at = {}
    after = {}  # template line -> hand-added leaves that belong inside that node
    extras = []
    for path, value in _flatten(overrides):
        node = _find(tree, path)
        if node is not None:
            value_at[node.line] = value
            _uncomment_chain(node, uncomment)
            continue
        parent = _find(tree, path[:-1])
        if parent is None:
            extras.append((path, value))
            continue
        # A hand-added key under a node the template DOES write goes inside that
        # node. In the Extra block it would open a SECOND `<node>:` mapping, and
        # PyYAML keeps only the last duplicate — every override the template
        # wrote for that node would be gone on the next read.
        _uncomment_chain(parent, uncomment)
        after.setdefault(parent.line, []).append(f"{'  ' * (parent.level + 1)}{path[-1]}: {_fmt(value)}")

    out = []
    for i, raw in enumerate(lines):
        if i in uncomment:
            line = raw[2:] if raw.startswith("# ") else raw
            out.append(_replace_value(line, value_at[i]) if i in value_at else line)
        else:
            out.append(raw)
        out.extend(after.get(i, ()))
    text = "\n".join(out)

    if extras:
        extra_dict = {}
        for path, value in extras:
            _set_path(extra_dict, list(path), value)
        text = text.rstrip("\n") + "\n\n# ── Extra overrides (not in the standard list above) ──\n"
        text += yaml.safe_dump(extra_dict, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return text


def _duplicate_top_level_keys(text: str) -> set:
    keys = [line.split(":", 1)[0] for line in text.split("\n") if line[:1].isalnum() and ":" in line]
    return {key for key in keys if keys.count(key) > 1}


def apply_changes(sets, clears):
    """Apply override *sets* (``[{path, value, type}]``) and *clears* (``[path]``),
    then regenerate a standardised settings.yaml. Returns ``(ok, message)``.

    Serialised under a lock so concurrent saves (e.g. two browser tabs) can't
    clobber each other's read-modify-write."""
    with _WRITE_LOCK:
        return _apply_changes_locked(sets, clears)


def _apply_changes_locked(sets, clears):
    overrides = read_overrides()
    for kp in clears:
        _remove_path(overrides, list(kp))
    for item in sets:
        _set_path(overrides, list(item["path"]), _coerce(item["value"], item.get("type", "string")))
    _prune_empty(overrides)

    text = _regenerate(overrides)
    try:
        yaml.safe_load(text)  # never write a file that won't parse
    except yaml.YAMLError as e:
        return False, f"refusing to write invalid YAML: {e}"
    # PyYAML keeps only the last of a duplicated key, so a file that carries one
    # silently loses the rest on the next read. Nothing reachable produces one any
    # more; refusing is still better than writing a file that eats overrides.
    duplicated = _duplicate_top_level_keys(text)
    if duplicated:
        return False, f"refusing to write duplicated settings for: {', '.join(sorted(duplicated))}"

    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"could not create {path.parent}: {e}"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"write failed: {e}"
    return True, "saved"
