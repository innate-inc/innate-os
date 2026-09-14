# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""GET /fleet.json — which ROS nodes live in which processes, and what each costs.

The robot's node fleet is spread over tmux panes -> launch supervisors -> node
processes, and nothing in ROS reports that shape: `ros2 node list` knows nodes but
not processes, `ps` knows processes but not nodes. This joins the two by walking
each tmux pane's process tree and reading /proc, so the webapp can show where the
8 GB actually goes.

PSS, not RSS, is the honest per-process number: RSS counts every shared page in
full against every process that maps it, so summing RSS over 40 ROS processes
double-counts libc, rclcpp and Zenoh dozens of times.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

from aiohttp import web

TMUX_SESSION = "ros_nodes"
CACHE_TTL_SEC = 4.0

_NODE_ARG = re.compile(r"__node:=(\S+)")
_cache: tuple[float, dict] | None = None


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _mem(pid: int) -> tuple[float, float, int]:
    """(pss_mb, rss_mb, threads) — zeros if the process exited mid-scan."""
    pss = rss = 0
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                pss = int(line.split()[1])
            elif line.startswith("Rss:"):
                rss = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        threads = len(os.listdir(f"/proc/{pid}/task"))
    except OSError:
        threads = 0
    return round(pss / 1024, 1), round(rss / 1024, 1), threads


def _children_map() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            # comm sits in parens and may itself contain spaces or ')', so the
            # fields after the LAST ')' are the only safe ones to index.
            ppid = int(stat[stat.rindex(")") + 1 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))
    return kids


def _run(args: list[str], timeout: float) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _node_multiplicity() -> dict[str, int]:
    """How many ROS nodes share each name — i.e. how many a process really hosts.

    Several nodes here spin in-process helpers that reuse the node name (the
    launch files mute the resulting "Publisher already registered" warning), so a
    name appearing 4x means one process hosting 4 nodes, not 4 processes.
    """
    counts: dict[str, int] = {}
    for line in _run(["ros2", "node", "list"], timeout=12).splitlines():
        name = line.strip().lstrip("/")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _container_components() -> dict[str, list[str]]:
    """{container_node_name: [component node names]} from `ros2 component list`."""
    out: dict[str, list[str]] = {}
    current = None
    for line in _run(["ros2", "component", "list"], timeout=12).splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            current = line.strip().lstrip("/")
            out[current] = []
        elif current:
            # "  1  /main_camera_driver"
            parts = line.split()
            if len(parts) >= 2:
                out[current].append(parts[-1].lstrip("/"))
    return out


_ROS_EXE = re.compile(r"/(?:install|opt/ros/\w+)/[^/]+/lib/[^/]+/([\w.]+)")
# argv is ".../bin/ros2 launch pkg file.py", so the verb is preceded by a path
# separator as often as by a space.
_ROS2_VERB = re.compile(r"(?:^|[/\s])ros2\s+(launch|run)\s")
_NS_ARG = re.compile(r"__ns:=(\S+)")


def _classify(cmd: str) -> str:
    if _ROS2_VERB.search(cmd):
        return "launch"
    if "component_container" in cmd:
        return "container"
    if "rmw_zenohd" in cmd:
        return "router"
    if "https_server.py" in cmd:
        return "webapp"
    if "__node:=" in cmd or _ROS_EXE.search(cmd):
        return "node"
    return "other"


def _label(cmd: str, kind: str) -> str:
    node = _NODE_ARG.search(cmd)
    if node:
        # A namespaced node registers as <ns>/<name>; match `ros2 node list`.
        ns = _NS_ARG.search(cmd)
        name = node.group(1).lstrip("/")
        return f"{ns.group(1).strip('/')}/{name}" if ns and ns.group(1).strip("/") else name
    if kind == "launch":
        match = _ROS2_VERB.search(cmd)
        verb = match.group(1) if match else "launch"
        tail = cmd[match.end() :].split() if match else []
        return f"ros2 {verb} " + " ".join(tail[:2]) if tail else f"ros2 {verb}"
    if kind == "webapp":
        return "webapp (https_server)"
    if kind == "router":
        return "zenoh router"
    exe = _ROS_EXE.search(cmd)
    if exe:
        return exe.group(1)
    return Path(cmd.split()[0]).name.lstrip("-") if cmd else "?"


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def _guess_node(cmd: str, unclaimed: set[str]) -> str | None:
    """Match a node launched by `ros2 run` (no __node:= to read) to its ROS name.

    The executable and the node it registers rarely match exactly
    (innate_training_node/training_node -> `innate_training`), so score the
    remaining unclaimed names by token overlap with the executable path and take
    a clear winner only.
    """
    want = _tokens(cmd)
    best, best_score = None, 0
    for name in unclaimed:
        score = len(_tokens(name) & want)
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 1 else None


def _walk(pid: int, kids: dict[int, list[int]], seen: set[int], out: list[dict]) -> None:
    """Collect the interesting processes under a pane, skipping shell wrappers."""
    if pid in seen:
        return
    seen.add(pid)
    cmd = _cmdline(pid)
    kind = _classify(cmd)
    # A login shell reports argv[0] as "-zsh", so strip the dash before matching.
    base = Path(cmd.split()[0]).name.lstrip("-") if cmd else ""
    # The pane's own shell and the `ros2` wrapper's shell layers carry no ROS
    # identity; recurse past them rather than listing them.
    if cmd and base not in ("zsh", "bash", "sh", "dash", "sleep", "tmux"):
        pss, rss, threads = _mem(pid)
        out.append(
            {
                "pid": pid,
                "kind": kind,
                "label": _label(cmd, kind),
                "cmd": cmd[:400],
                "pss_mb": pss,
                "rss_mb": rss,
                "threads": threads,
            }
        )
    for child in kids.get(pid, []):
        _walk(child, kids, seen, out)


def _panes() -> list[dict]:
    fmt = "#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_pid}\t#{pane_start_command}"
    raw = _run(["tmux", "list-panes", "-s", "-t", TMUX_SESSION, "-F", fmt], timeout=5)
    panes = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[3].isdigit():
            continue
        panes.append(
            {
                "window_index": int(parts[0]),
                "window": parts[1],
                "pane_index": int(parts[2]),
                "pane_pid": int(parts[3]),
            }
        )
    return panes


def _meminfo() -> dict:
    vals = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            vals[key] = int(rest.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return {}
    return {
        "total_mb": vals.get("MemTotal", 0),
        "available_mb": vals.get("MemAvailable", 0),
        "used_mb": vals.get("MemTotal", 0) - vals.get("MemAvailable", 0),
    }


def collect() -> dict:
    kids = _children_map()
    multiplicity = _node_multiplicity()
    components = _container_components()

    windows: dict[int, dict] = {}
    seen: set[int] = set()
    total_pss = 0.0
    total_procs = 0
    total_nodes = 0

    unclaimed = set(multiplicity)
    for pane in _panes():
        procs: list[dict] = []
        _walk(pane["pane_pid"], kids, seen, procs)
        for proc in procs:
            label = proc["label"]
            if proc["kind"] == "container":
                proc["nodes"] = [label, *components.get(label, [])]
            elif proc["kind"] == "node":
                if label not in multiplicity:
                    # Launched by `ros2 run`: the label came off the executable
                    # path, which is not what the node registered itself as.
                    guess = _guess_node(proc["cmd"], unclaimed)
                    if guess:
                        proc["label"] = label = guess
                        proc["inferred_name"] = True
                # One entry per node the process actually hosts; duplicates in
                # `ros2 node list` are in-process helpers sharing the name.
                proc["nodes"] = [label] * max(1, multiplicity.get(label, 1))
            else:
                proc["nodes"] = []
            unclaimed -= set(proc["nodes"])
            total_pss += proc["pss_mb"]
            total_procs += 1
            total_nodes += len(proc["nodes"])

        win = windows.setdefault(
            pane["window_index"],
            {"index": pane["window_index"], "name": pane["window"], "panes": []},
        )
        win["panes"].append(
            {
                "index": pane["pane_index"],
                "pid": pane["pane_pid"],
                "processes": sorted(procs, key=lambda p: -p["pss_mb"]),
            }
        )

    # nav2 names its in-process helper nodes after their owner
    # (bt_navigator -> bt_navigator_navigate_to_pose_rclcpp_node), so a leftover
    # that extends an attributed name belongs to that process.
    all_procs = [p for win in windows.values() for pane in win["panes"] for p in pane["processes"]]
    for proc in sorted(all_procs, key=lambda p: -len(p["label"])):
        if not proc["nodes"]:
            continue
        owned = {n for n in unclaimed if n.startswith(proc["label"]) and n != proc["label"]}
        if owned:
            proc["nodes"].extend(sorted(owned))
            proc["helpers"] = sorted(owned)
            unclaimed -= owned
            total_nodes += len(owned)

    ordered = [windows[k] for k in sorted(windows)]
    for win in ordered:
        win["panes"].sort(key=lambda p: p["index"])
        win["pss_mb"] = round(sum(p["pss_mb"] for pane in win["panes"] for p in pane["processes"]), 1)

    return {
        "host": os.uname().nodename,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session": TMUX_SESSION,
        "mem": _meminfo(),
        "totals": {
            "processes": total_procs,
            "nodes": total_nodes,
            "pss_mb": round(total_pss, 1),
            "windows": len(ordered),
        },
        "windows": ordered,
        # Nodes ROS reports that no pane process claimed — nothing is hiding, but
        # say so rather than letting the totals quietly disagree with `ros2 node list`.
        "unattributed": sorted(unclaimed),
    }


async def fleet_response(request: web.Request) -> web.Response:
    """GET /fleet.json -> the node/process topology, cached briefly.

    The ros2 CLI calls behind this take seconds, so a reload storm (or several
    open tabs) must not queue up one scan each.
    """
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_TTL_SEC and "nocache" not in request.query:
        return web.json_response(_cache[1], headers={"Cache-Control": "no-cache"})
    data = await asyncio.to_thread(collect)
    _cache = (now, data)
    return web.json_response(data, headers={"Cache-Control": "no-cache"})
