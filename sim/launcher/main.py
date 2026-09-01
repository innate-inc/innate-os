#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import argparse
import subprocess
import sys

if sys.version_info < (3, 10):  # noqa: UP036
    print("Error: the Innate launcher requires Python 3.10 or newer.", file=sys.stderr)
    raise SystemExit(1)

from config import (
    CLI_SIM,
    ENV_PATH,
    LOG_TARGETS,
    NO_BACKEND,
    OS_SESSION_LOG_PATH,
    SETTINGS_PATH,
    SHOW_LIVE_DASHBOARD_DEFAULT,
    SIM_CONFIG_PATH,
    STATE_DIR,
    StackError,
    build_os_env,
    get_config,
    log,
    success,
    warn,
)
from dashboard import (
    BOLD,
    NC,
    DashboardCallbacks,
    DashboardOptions,
    live_step,
    print_banner,
    print_status,
    watch_dashboard,
)
from runtime import (
    capture_os_brain_logs,
    clean_runtime,
    collect_status_snapshot,
    down_os,
    ensure_docker_available,
    ensure_os_container,
    ensure_sim_assets,
    ensure_sim_viewer_bundle,
    ensure_skill_assets,
    ensure_uv_available,
    ensure_viewer_public_assets,
    ensure_workspace_dirs,
    ensure_world_server,
    open_os_container_shell,
    prefetch_runtime,
    print_startup_checks,
    refuse_if_ports_taken,
    remove_superseded_containers,
    runtime_already_running,
    stop_world_server,
    tail_file,
    wait_for_os_runtime_ready,
    wait_for_virtual_mars,
    world_server_running,
)
from setup_wizard import (
    BRAIN_BACKENDS,
    _prompt_yes_no,
    apply_brain_backend,
    configure_brain_backend,
    ensure_uv_prerequisite,
    is_interactive_terminal,
    report_configured_keys,
)

DASHBOARD_OPTIONS = DashboardOptions(
    cli_sim=CLI_SIM,
    state_dir=STATE_DIR,
)


def dashboard_callbacks() -> DashboardCallbacks:
    return DashboardCallbacks(
        collect_status_snapshot=collect_status_snapshot,
        capture_os_brain_logs=capture_os_brain_logs,
        success=success,
    )


def show_runtime_dashboard(config: dict[str, object], *, watch: bool) -> None:
    if watch and sys.stdout.isatty():
        dashboard_result = watch_dashboard(config, dashboard_callbacks(), DASHBOARD_OPTIONS)
        if dashboard_result == "shutdown":
            print()
            log("Ctrl+C received. Stopping the Innate runtime...")
            cmd_down(config)
    else:
        print_status(config, dashboard_callbacks(), DASHBOARD_OPTIONS)


def cmd_up(
    config: dict[str, object],
    *,
    watch: bool = SHOW_LIVE_DASHBOARD_DEFAULT,
    offline: bool = False,
) -> None:
    started = False
    try:
        # Banner before any probe: a wedged Docker daemon must never leave
        # the user staring at a blank terminal.
        print_banner()
        ensure_docker_available(command_hint=f"{CLI_SIM} up")
        ensure_uv_available()  # the sim world always runs on the host via uv
        report_configured_keys(config)
        # Before anything containerized runs: claims the container-written
        # workspace dirs for the invoking user (root-owned bind-mount dirs on
        # Linux otherwise), and warns if an earlier run already claimed them.
        ensure_workspace_dirs(config)
        # Before the fast path, not after it: the containers it removes are
        # exactly what an upgrade from a still-running older stack leaves
        # behind -- and one of them holds the ports this stack needs.
        remove_superseded_containers()
        if runtime_already_running(config):
            # A code update can leave a stale world server running (frozen
            # 3D view); ensure_world_server restarts it.
            ensure_world_server(config)
            log("Innate sim runtime is already running. Opening dashboard...")
            show_runtime_dashboard(config, watch=watch)
            return
        refuse_if_ports_taken()

        os_env_file = build_os_env(config)
        if offline:
            log("Offline: skipping sim/skill asset downloads.")
        else:
            try:
                with live_step("assets", "Downloading the world geometry", "world geometry"):
                    ensure_sim_assets(config)
                with live_step("skills", "Downloading the skill assets", "skill assets"):
                    ensure_skill_assets(config)
            except StackError as exc:
                raise StackError(
                    f"{exc}\n\n"
                    "This step needs internet access. Re-run with a connection, or re-run "
                    f"`{CLI_SIM} up --offline` to start with whatever is already downloaded."
                ) from exc
        with live_step("viewer", "Downloading the 3D view assets", "3D view assets"):
            ensure_viewer_public_assets(config)
        with live_step("bundle", "Fetching the 3D viewer bundle", "3D viewer bundle"):
            ensure_sim_viewer_bundle(config, offline=offline)
        with live_step("world", "Starting the physics world", "physics world"):
            config["world_endpoint"] = ensure_world_server(config)

        started = True
        try:
            with live_step("os", "Starting the Innate OS container", "Innate OS container"):
                ensure_os_container(config, os_env_file, offline=offline)
        except StackError as exc:
            if offline:
                raise
            raise StackError(
                f"{exc}\n\n"
                "This is a Docker pull/build that needs the network. If you have started the "
                f"runtime successfully before and are now offline, re-run `{CLI_SIM} up --offline` "
                "to reuse the existing images instead of pulling/building."
            ) from exc

        # Startup is dominated by ROS node bring-up (the workspace build
        # cache is warm), so give the nodes real time to come up.
        with live_step("brain", "Waiting for the ROS bridge and brain client", "ROS bridge and brain client") as step:
            step.ok = wait_for_os_runtime_ready(config, timeout_seconds=120.0)
        if not step.ok:
            print_startup_checks(config, sim_driver_ready=False)
            raise StackError(
                "The OS ROS bridge/brain client did not become ready.\n"
                f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
            )
        with live_step("sim", "Waiting for the sim driver (/odom)", "sim driver (/odom)") as step:
            sim_driver_ready = step.ok = wait_for_virtual_mars(config)
        world_alive = print_startup_checks(config, sim_driver_ready=sim_driver_ready)
        if not world_alive:
            # It passed the startup gate and died during boot -- on small
            # machines that is almost always the OOM killer's work.
            warn("The world server died during startup. On low-memory machines this is usually")
            warn("the OOM killer (check: sudo dmesg | grep -i oom). Free up memory or add swap,")
            warn(f"then restart: {CLI_SIM} down && {CLI_SIM} up. Log: {CLI_SIM} logs world-server")
            warn(f"The runtime is left running for inspection; stop it with `{CLI_SIM} down`.")
            return
        if not sim_driver_ready:
            # Leave the runtime up so the failure can be inspected.
            warn("The sim driver (MuJoCo) never started publishing /odom.")
            warn(
                f"The runtime is left running for inspection: `{CLI_SIM} sh`, then "
                "`tmux attach -t innate` and check the 'sim-driver' window. "
                f"Stop everything with `{CLI_SIM} down`."
            )
            return
        if config["brain_backend"] == NO_BACKEND:
            warn("No cloud LLM key configured — the sim is running WITHOUT an agent.")
            warn(
                "Add GEMINI_API_KEY (your own Gemini key) or INNATE_SERVICE_KEY (Innate proxy) to "
                f"{ENV_PATH}, or run `{CLI_SIM} setup`, then restart."
            )
        success("Innate sim runtime is up.")
        show_runtime_dashboard(config, watch=watch)
    except KeyboardInterrupt:
        print()
        if started:
            warn("Interrupted. Stopping the Innate runtime...")
            cmd_down(config)
        else:
            warn("Interrupted before the Innate runtime finished starting.")
    except StackError as exc:
        if started:
            # Show the real failure before cleanup: `docker compose down` can
            # take a while (or misbehave), and the error must not wait on it.
            print(f"Error: {exc}", file=sys.stderr)
            warn("Startup failed. Stopping the partially-started Innate runtime...")
            cmd_down(config)
            raise SystemExit(1) from exc
        raise


def cmd_down(config: dict[str, object]) -> None:
    remove_superseded_containers()
    down_os(config)
    stop_world_server()
    log("Innate sim runtime is down.")


def _confirm_clean() -> bool:
    print(f"{BOLD}This will permanently delete:{NC}")
    print("  - Docker containers and volumes for the sim runtime")

    if not is_interactive_terminal():
        warn("Refusing to clean without confirmation. Re-run with --yes to proceed non-interactively.")
        return False

    return _prompt_yes_no("Continue?", default=False)


def cmd_clean(config: dict[str, object], *, assume_yes: bool = False) -> None:
    if not assume_yes and not _confirm_clean():
        warn("Aborted. Nothing was deleted.")
        return

    stop_world_server()
    clean_runtime(config)
    success("Innate sim runtime cleaned (containers and volumes removed).")

    print("Preserved (never deleted by clean):")
    print(f"  - secrets:      {ENV_PATH}")
    print(f"  - OS config:    {SETTINGS_PATH}")
    print(f"  - sim config:   {SIM_CONFIG_PATH}")

    log(f"Run `{CLI_SIM} up` to start the runtime again.")


def cmd_logs(target: str, lines: int | None = None) -> None:
    if target == "startup":
        found_logs = False
        for name in ("bootstrap", "world-server", "compose", "os-build", "viewer-build", "os-session"):
            path = LOG_TARGETS[name]
            if path.exists():
                found_logs = True
                print(f"{BOLD}{path}{NC}")
                print(tail_file(path, limit=lines or 80))
                print()
        if not found_logs:
            warn("No startup logs have been written yet.")
        return

    if target == "brain":
        config = get_config()
        print("\n".join(capture_os_brain_logs(config, lines=lines or 60)))
        return

    path = LOG_TARGETS[target]
    print(tail_file(path, limit=lines or 120))


def cmd_setup(
    config: dict[str, object],
    *,
    prefetch: bool = True,
    configure: bool = True,
    backend: str | None = None,
) -> None:
    """Ask which key the agent thinks with, then download what `up` would.

    The two halves are separable because the installer asks everything before
    it installs anything: it runs `--no-prefetch` right after the clone, while
    the user is still at the keyboard, and `--prefetch-only` once Docker is in
    place -- by which time nobody has to be watching.
    """
    print_banner()
    if backend:
        # The installer asked before it installed anything; the key arrives on
        # stdin so it never becomes a temp file or an entry in `ps`.
        apply_brain_backend(config, backend, sys.stdin.read().strip() if backend != "none" else "")
    elif configure:
        configure_brain_backend(config)
    if prefetch:
        ensure_docker_available(command_hint=f"{CLI_SIM} setup")
        ensure_uv_prerequisite()
        prefetch_runtime(config)
    success("Simulator setup is ready." if prefetch else "Keys saved.")
    print(f"OS secrets: {ENV_PATH}")
    if prefetch:
        print(f"Sim config: {SIM_CONFIG_PATH}")
        log(f"Start the simulator with `{CLI_SIM} up`.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="innate-sim", description="Innate local simulator CLI.")
    sim_subparsers = parser.add_subparsers(dest="sim_command", required=True)
    setup_parser = sim_subparsers.add_parser(
        "setup",
        prog=f"{CLI_SIM} setup",
        help="Prepare the simulator: prerequisites, agent keys, and the runtime download",
    )
    setup_parser.add_argument(
        "--no-prefetch",
        action="store_true",
        help="Configure keys only; leave the images and assets for the first `up` to download",
    )
    setup_parser.add_argument(
        "--backend",
        choices=BRAIN_BACKENDS,
        help="Apply a cloud-LLM choice made elsewhere, reading the key from stdin (used by the installer)",
    )
    setup_parser.add_argument(
        "--prefetch-only",
        action="store_true",
        help="Download the runtime without asking about keys again (they are already set)",
    )
    up_parser = sim_subparsers.add_parser(
        "up",
        prog=f"{CLI_SIM} up",
        help="Start the local simulator-backed runtime",
    )
    up_parser.add_argument(
        "--once",
        action="store_true",
        help="Start the runtime and print a single status snapshot instead of the live dashboard",
    )
    up_parser.add_argument(
        "--offline",
        action="store_true",
        help="Run without network: skip skill asset downloads, and reuse already-built Docker images instead of pulling/building",
    )
    sim_subparsers.add_parser(
        "down",
        prog=f"{CLI_SIM} down",
        help="Stop the running container and world server (keeps data; use `clean` to remove volumes)",
    )
    sim_subparsers.add_parser(
        "assets",
        prog=f"{CLI_SIM} assets",
        help="Download/refresh the sim asset bundle only (no Docker) -- for VirtualMars/notebook use",
    )
    clean_parser = sim_subparsers.add_parser(
        "clean",
        prog=f"{CLI_SIM} clean",
        help="Stop the runtime and delete related Docker containers/volumes",
    )
    clean_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for non-interactive/scripted use)",
    )
    sim_subparsers.add_parser(
        "sh",
        prog=f"{CLI_SIM} sh",
        help="Open an interactive shell inside the running ROS container",
    )
    status_parser = sim_subparsers.add_parser(
        "status",
        prog=f"{CLI_SIM} status",
        help="Show current runtime status",
    )
    status_parser.add_argument(
        "mode",
        nargs="?",
        default="panel",
        choices=["panel", "verbose"],
        help="Show the default panel or include extra repo/runtime details",
    )
    logs_parser = sim_subparsers.add_parser(
        "logs",
        prog=f"{CLI_SIM} logs",
        help="Show recent logs",
    )
    logs_parser.add_argument(
        "target",
        # Derived from LOG_TARGETS so a new log stream can't be forgotten
        # here again (world-server was documented but missing).
        choices=["startup", "brain", *sorted(LOG_TARGETS)],
        help="Which log stream to show",
    )
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=None,
        help="Number of lines to show (overrides the per-stream default)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:])

    try:
        config = get_config()

        if args.sim_command == "setup":
            cmd_setup(
                config,
                prefetch=not args.no_prefetch,
                configure=not args.prefetch_only,
                backend=args.backend,
            )
        elif args.sim_command == "up":
            cmd_up(
                config,
                watch=not args.once,
                offline=args.offline,
            )
        elif args.sim_command == "down":
            ensure_docker_available(command_hint=f"{CLI_SIM} down")
            cmd_down(config)
        elif args.sim_command == "assets":
            # Pure download+extract: VirtualMars (scripts/notebooks, no ROS,
            # no Docker) needs sim/assets without bringing the stack up. If a
            # world server IS running, reconcile it like `up` would -- its
            # MuJoCo model was compiled from the previous bundle, and leaving
            # it serving stale collision physics is worse than a restart.
            ensure_sim_assets(config)
            # A refresh replaces the geometry under a running world server, so
            # reconcile it.
            if world_server_running():
                ensure_world_server(config)
            success("Sim assets are in place (sim/assets).")
        elif args.sim_command == "clean":
            ensure_docker_available(command_hint=f"{CLI_SIM} clean")
            cmd_clean(config, assume_yes=args.yes)
        elif args.sim_command == "sh":
            # Opens the running container with `docker exec`, so a missing
            # Compose plugin must not block it.
            ensure_docker_available(command_hint=f"{CLI_SIM} sh", require_compose=False)
            return open_os_container_shell()
        elif args.sim_command == "status":
            ensure_docker_available(command_hint=f"{CLI_SIM} status")
            print_status(
                config,
                dashboard_callbacks(),
                DASHBOARD_OPTIONS,
                verbose=args.mode == "verbose",
            )
        elif args.sim_command == "logs":
            cmd_logs(args.target, args.lines)
        else:
            parser.error(f"Unknown sim command: {args.sim_command}")
    except StackError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # e.g. a full disk that flipped the filesystem read-only (seen in a
        # user test): one actionable line, not a traceback.
        print(
            f"Error: {exc}\n"
            "This is a filesystem problem, not an Innate one -- check free disk space "
            "(a full disk can leave the filesystem mounted read-only until a reboot).",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Command failed: {' '.join(exc.cmd)}", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        if exc.stderr:
            print(exc.stderr, file=sys.stderr)
        return exc.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
