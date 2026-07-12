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
    HOSTED_MODE,
    LOCAL_MODES,
    LOG_TARGETS,
    NONE_MODE,
    OS_SESSION_LOG_PATH,
    SETTINGS_PATH,
    SHOW_LIVE_DASHBOARD_DEFAULT,
    SIM_CONFIG_PATH,
    STATE_DIR,
    StackError,
    build_cloud_env,
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
    print_banner,
    print_status,
    watch_dashboard,
)
from runtime import (
    capture_agent_logs,
    capture_os_brain_logs,
    clean_runtime,
    collect_status_snapshot,
    down_cloud_agent,
    down_os,
    ensure_docker_available,
    ensure_os_container,
    ensure_sim_assets,
    ensure_sim_viewer_bundle,
    ensure_skill_assets,
    ensure_workspace_dirs,
    ensure_world_server,
    open_os_container_shell,
    print_startup_checks,
    runtime_already_running,
    start_cloud_agent,
    stop_world_server,
    tail_file,
    wait_for_os_runtime_ready,
    wait_for_virtual_mars,
)
from setup_wizard import (
    _prompt_yes_no,
    configure_brain_backend,
    is_interactive_terminal,
    report_configured_keys,
)

DASHBOARD_OPTIONS = DashboardOptions(
    hosted_mode=HOSTED_MODE,
    local_modes=LOCAL_MODES,
    cli_sim=CLI_SIM,
    state_dir=STATE_DIR,
)


def dashboard_callbacks() -> DashboardCallbacks:
    return DashboardCallbacks(
        collect_status_snapshot=collect_status_snapshot,
        capture_os_brain_logs=capture_os_brain_logs,
        capture_agent_logs=capture_agent_logs,
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
        report_configured_keys(config)
        # Before anything containerized runs: claims the container-written
        # workspace dirs for the invoking user (root-owned bind-mount dirs on
        # Linux otherwise), and warns if an earlier run already claimed them.
        ensure_workspace_dirs(config)
        if runtime_already_running(config):
            # A code update can leave a stale world server running (frozen
            # 3D view); ensure_world_server restarts it.
            ensure_world_server(config)
            log("Innate sim runtime is already running. Opening dashboard...")
            show_runtime_dashboard(config, watch=watch)
            return

        os_env_file = build_os_env(config)
        cloud_env_file = build_cloud_env(config)
        if offline:
            log("Offline: skipping sim/skill asset downloads.")
        else:
            try:
                ensure_sim_assets(config)
                ensure_skill_assets(config)
            except StackError as exc:
                raise StackError(
                    f"{exc}\n\n"
                    "This step needs internet access. Re-run with a connection, or re-run "
                    f"`{CLI_SIM} up --offline` to start with whatever is already downloaded."
                ) from exc
        ensure_sim_viewer_bundle(config, offline=offline)
        config["world_endpoint"] = ensure_world_server(config)

        started = True
        try:
            start_cloud_agent(config, cloud_env_file)
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

        log("Waiting for ROS bridge and brain client...")
        # Startup is dominated by ROS node bring-up (the workspace build
        # cache is warm), so give the nodes real time to come up.
        if not wait_for_os_runtime_ready(config, timeout_seconds=120.0):
            print_startup_checks(config, sim_driver_ready=False)
            raise StackError(
                "The OS ROS bridge/brain client did not become ready.\n"
                f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
            )
        log("Waiting for the sim driver (/odom)...")
        sim_driver_ready = wait_for_virtual_mars(config)
        print_startup_checks(config, sim_driver_ready=sim_driver_ready)
        if not sim_driver_ready:
            # Leave the runtime up so the failure can be inspected.
            warn("The sim driver (MuJoCo) never started publishing /odom.")
            warn(
                f"The runtime is left running for inspection: `{CLI_SIM} sh`, then "
                "`tmux attach -t innate` and check the 'sim-driver' window. "
                f"Stop everything with `{CLI_SIM} down`."
            )
            return
        if config["mode"] == NONE_MODE:
            warn("No brain backend configured — the sim is running WITHOUT an agent.")
            warn(
                "Add GEMINI_API_KEY (local brain) or INNATE_SERVICE_KEY (hosted) to "
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
    down_cloud_agent()
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
        for name in ("bootstrap", "compose", "cloud-agent", "os-build", "viewer-build", "os-session"):
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

    if target == "cloud-agent":
        # Live container logs, matching the dashboard's agent pane. The build/startup
        # output is still available via `logs startup`.
        config = get_config()
        print("\n".join(capture_agent_logs(config, lines=lines or 60)))
        return

    path = LOG_TARGETS[target]
    print(tail_file(path, limit=lines or 120))


def cmd_setup(config: dict[str, object]) -> None:
    print_banner()
    ensure_docker_available(command_hint=f"{CLI_SIM} setup")
    configure_brain_backend(config)
    success("Simulator setup is ready.")
    print(f"OS secrets: {ENV_PATH}")
    print(f"Sim config: {SIM_CONFIG_PATH}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="innate-sim", description="Innate local simulator CLI.")
    sim_subparsers = parser.add_subparsers(dest="sim_command", required=True)
    sim_subparsers.add_parser(
        "setup",
        prog=f"{CLI_SIM} setup",
        help="Prepare the simulator runtime credentials",
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
        help="Stop the local simulator-backed runtime",
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
        choices=[
            "startup",
            "bootstrap",
            "compose",
            "cloud-agent",
            "os-build",
            "viewer-build",
            "os-session",
            "brain",
            "down",
        ],
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
            cmd_setup(config)
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
            # no Docker) needs sim/assets without bringing the stack up.
            ensure_sim_assets(config)
            success("Sim assets are in place (sim/assets + sim/viewer).")
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
