#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys

if sys.version_info < (3, 11):
    print("Error: the Innate launcher requires Python 3.11 or newer.", file=sys.stderr)
    raise SystemExit(1)

from dashboard import (
    BOLD,
    NC,
    DashboardCallbacks,
    DashboardOptions,
    print_banner,
    print_status,
    watch_dashboard,
)
from config import (
    CLI_ROOT,
    CLI_SIM,
    ENV_PATH,
    LOG_TARGETS,
    OS_CONFIG_PATH,
    OS_SESSION_LOG_PATH,
    SIM_CONFIG_PATH,
    SHOW_LIVE_DASHBOARD_DEFAULT,
    STATE_DIR,
    HOSTED_MODE,
    LOCAL_MODES,
    StackError,
    build_cloud_env,
    build_os_env,
    get_config,
    log,
    success,
    warn,
)
from runtime import (
    available_agent_count,
    capture_agent_logs,
    capture_os_brain_logs,
    capture_simulator_logs,
    collect_status_snapshot,
    config_simulator_port,
    ensure_dependency,
    ensure_os_container,
    ensure_sim_data,
    ensure_sim_setup,
    print_startup_checks,
    runtime_already_running,
    set_simulator_log_mode,
    start_cloud_agent,
    start_simulator,
    stop_simulator,
    down_cloud_agent,
    down_os,
    tail_file,
    wait_for_brain_directives,
    wait_for_os_runtime_ready,
    wait_for_simulator_http,
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
        capture_simulator_logs=capture_simulator_logs,
        capture_os_brain_logs=capture_os_brain_logs,
        capture_agent_logs=capture_agent_logs,
        set_simulator_log_mode=set_simulator_log_mode,
        success=success,
    )


def show_runtime_dashboard(config: dict[str, object], *, watch: bool) -> None:
    if watch and sys.stdout.isatty():
        dashboard_result = watch_dashboard(
            config, dashboard_callbacks(), DASHBOARD_OPTIONS
        )
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
    sim_visualization_override: bool | None = None,
) -> None:
    started = False
    try:
        if sim_visualization_override is not None:
            config = {**config, "sim_visualization": sim_visualization_override}
        print_banner()
        ensure_dependency("docker")
        if runtime_already_running(config):
            log("Innate sim runtime is already running. Opening dashboard...")
            show_runtime_dashboard(config, watch=watch)
            return

        os_env_file = build_os_env(config)
        cloud_env_file = build_cloud_env(config)
        sim_python = ensure_sim_setup(config, allow_setup=False)
        ensure_sim_data(config, allow_fetch=False)

        started = True
        start_cloud_agent(config, cloud_env_file)
        ensure_os_container(config, os_env_file)
        start_simulator(config, sim_python)

        simulator_port = config_simulator_port(config)
        log("Waiting for the simulator HTTP endpoint...")
        wait_for_simulator_http(simulator_port)
        log("Waiting for ROS bridge and brain client...")
        if not wait_for_os_runtime_ready(config):
            print_startup_checks(
                config,
                simulator_http_ready=True,
                brain_directive_count=available_agent_count(simulator_port),
            )
            raise StackError(
                "Simulator backend is up, but the OS ROS bridge/brain client did not become ready.\n"
                f"Recent OS log output:\n{tail_file(OS_SESSION_LOG_PATH, limit=80)}"
            )
        log("Waiting for brain directives...")
        brain_directive_count = wait_for_brain_directives(simulator_port)
        print_startup_checks(
            config,
            simulator_http_ready=True,
            brain_directive_count=brain_directive_count,
        )
        if brain_directive_count <= 0:
            raise StackError(
                "Simulator backend is up, but brain directives never became available.\n"
                f"Recent brain log output:\n{os.linesep.join(capture_os_brain_logs(config, lines=40))}"
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
    except StackError:
        if started:
            warn("Startup failed. Stopping the partially-started Innate runtime...")
            cmd_down(config)
        raise


def cmd_down(config: dict[str, object]) -> None:
    stop_simulator()
    down_cloud_agent()
    down_os(config)
    log("Innate sim runtime is down.")


def cmd_logs(target: str) -> None:
    if target == "startup":
        found_logs = False
        for name in ("bootstrap", "frontend", "compose", "cloud-agent", "os-build", "os-session", "simulator"):
            path = LOG_TARGETS[name]
            if path.exists():
                found_logs = True
                print(f"{BOLD}{path}{NC}")
                print(tail_file(path, limit=80))
                print()
        if not found_logs:
            warn("No startup logs have been written yet.")
        return

    if target == "brain":
        config = get_config()
        print("\n".join(capture_os_brain_logs(config, lines=60)))
        return

    path = LOG_TARGETS[target]
    print(tail_file(path, limit=120))


def cmd_setup(config: dict[str, object]) -> None:
    print_banner()
    sim_python = ensure_sim_setup(config, allow_setup=True)
    ensure_sim_data(config, allow_fetch=True)
    success("Simulator setup is ready.")
    print(f"OS secrets: {ENV_PATH}")
    print(f"OS config: {OS_CONFIG_PATH}")
    print(f"Sim config: {SIM_CONFIG_PATH}")
    print(f"Simulator Python: {sim_python}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="innate",
        description="Innate local development CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    sim_parser = subparsers.add_parser(
        "sim",
        prog=f"{CLI_ROOT} sim",
        help="Set up and run the local simulator-backed runtime",
    )
    sim_subparsers = sim_parser.add_subparsers(dest="sim_command", required=True)
    sim_subparsers.add_parser(
        "setup",
        prog=f"{CLI_SIM} setup",
        help="Prepare the simulator environment, frontend build, and required scene data",
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
        "--vis",
        action="store_true",
        help="Start the simulator with the native visualization window enabled for this run",
    )
    sim_subparsers.add_parser(
        "down",
        prog=f"{CLI_SIM} down",
        help="Stop the local simulator-backed runtime",
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
        nargs="?",
        default="simulator",
        choices=["startup", "bootstrap", "frontend", "compose", "cloud-agent", "os-build", "os-session", "simulator", "brain", "down"],
        help="Which log stream to show",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:])

    try:
        config = get_config()

        if args.command != "sim":
            parser.error(f"Unknown command group: {args.command}")

        if args.sim_command == "setup":
            cmd_setup(config)
        elif args.sim_command == "up":
            cmd_up(
                config,
                watch=not args.once,
                sim_visualization_override=True if args.vis else None,
            )
        elif args.sim_command == "down":
            cmd_down(config)
        elif args.sim_command == "status":
            print_status(
                config,
                dashboard_callbacks(),
                DASHBOARD_OPTIONS,
                verbose=args.mode == "verbose",
            )
        elif args.sim_command == "logs":
            cmd_logs(args.target)
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
