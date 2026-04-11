#!/usr/bin/env python3
"""
CLI for the Innate Training Client.

Usage:
    python -m training_client.cli submit [SKILL_DIR] --server URL --token TOKEN
    python -m training_client.cli upload [SKILL_DIR] --server URL --token TOKEN
    python -m training_client.cli run [SKILL_DIR] --server URL --token TOKEN [training params]
    python -m training_client.cli status [SKILL_DIR] RUN_ID --server URL --token TOKEN
    python -m training_client.cli watch [SKILL_DIR] RUN_ID --server URL --token TOKEN
    python -m training_client.cli download [SKILL_DIR] RUN_ID --server URL --token TOKEN
    python -m training_client.cli activate [SKILL_DIR] RUN_ID
    python -m training_client.cli skills --server URL --token TOKEN
    python -m training_client.cli runs [SKILL_DIR] --server URL --token TOKEN

SKILL_DIR defaults to the current directory.  The skill ID is read from
SKILL_DIR/metadata.json (written by `submit`).

Example — create a skill, then start a training run:

    export TRAINING_SERVER_URL="https://my-dev-5ad29ff8.loca.lt"
    export INNATE_SERVICE_KEY="dev_innate_service_key_abcde"

    # Step 1: Create skill and upload data
    python -m training_client.cli submit ./skill

    # Step 2: Create a run with training params
    python -m training_client.cli run ./skill \
        --repo innate-inc/ACT-test \
        --ref lambda_refactor \
        -c "pip install -r requirements.txt; python3 -m act_test.train_dist --data-dir /data/dataset" \
        --hours 8 --gpu-type H100 --min-gpus 1 --max-gpus 2 \
        --budget 200

Quick start with a preset (all options pre-configured server-side):

    python -m training_client.cli submit ./skill
    python -m training_client.cli run ./skill --preset act-default

    # Override specific preset values:
    python -m training_client.cli run ./skill --preset act-default --max-gpus 4 --budget 500
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import click

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(
        logging, os.environ.get("LOG_LEVEL", "WARNING").upper(), logging.WARNING
    ),
    format="%(levelname)s %(name)s: %(message)s",
)

from training_client.src.skill_manager import SkillManager, read_skill_id
from training_client.src.types import (
    ClientConfig,
    DEFAULT_AUTH_ISSUER_URL,
    DEFAULT_SERVER_URL,
    ProgressStage,
    ProgressUpdate,
)


def _make_manager(ctx: click.Context) -> SkillManager:
    config = ClientConfig(
        server_url=ctx.obj["server"],
        auth_token=ctx.obj["token"],
        auth_issuer_url=ctx.obj.get("issuer", ""),
        poll_interval_seconds=ctx.obj.get("interval", 20.0),
    )
    return SkillManager(config)


def _resolve_skill_id(skill_dir: str | None) -> str:
    """Return a skill_id from metadata.json in skill_dir."""
    d = Path(skill_dir or ".").resolve()
    if not d.is_dir():
        raise click.BadParameter(f"Not a directory: {d}")
    sid = read_skill_id(d)
    if not sid:
        raise click.UsageError(
            f"No training_skill_id found in {d}/metadata.json. " "Run `submit` first."
        )
    return sid


def _same_except_bytes(a: ProgressUpdate | None, b: ProgressUpdate) -> bool:
    """Return True if *a* and *b* differ only in file_progress.bytes_done.

    The message field is intentionally excluded because it typically embeds
    the byte counts (e.g. "13.6/250.2 MB") and would always differ.
    """
    if a is None:
        return False
    if (a.stage, a.skill_id, a.run_id, a.error) != (
        b.stage,
        b.skill_id,
        b.run_id,
        b.error,
    ):
        return False
    fa, fb = a.file_progress, b.file_progress
    if fa is None or fb is None:
        return False
    return (fa.filename, fa.index, fa.total, fa.bytes_total, fa.done, fa.error) == (
        fb.filename,
        fb.index,
        fb.total,
        fb.bytes_total,
        fb.done,
        fb.error,
    )


def _print_progress(
    updates: Generator[ProgressUpdate, None, Any],
) -> ProgressUpdate | None:
    """Consume a progress generator, printing each update to stdout."""
    result: ProgressUpdate | None = None
    overwriting = False
    try:
        for update in updates:
            prefix = update.stage.value.upper().ljust(16)
            line = f"  {prefix} {update.message}"
            if _same_except_bytes(result, update):
                click.echo(f"\r{line}", nl=False)
                overwriting = True
            else:
                if overwriting:
                    click.echo()  # finish the previous \r line
                    overwriting = False
                click.echo(line)
            if update.error:
                click.secho(f"  {'ERROR'.ljust(16)} {update.error}", fg="red")
            result = update
    except StopIteration as e:
        result = e.value
    if overwriting:
        click.echo()  # finish the last \r line
    return result


# ── CLI Group ───────────────────────────────────────────────────────


@click.group()
@click.option(
    "--server",
    "-s",
    envvar="TRAINING_SERVER_URL",
    default=DEFAULT_SERVER_URL,
    show_default=True,
    help="Orchestrator URL (or set TRAINING_SERVER_URL)",
)
@click.option(
    "--token",
    "-t",
    required=True,
    envvar="INNATE_SERVICE_KEY",
    help="Auth token / service key (or set INNATE_SERVICE_KEY)",
)
@click.option(
    "--issuer",
    envvar="INNATE_AUTH_ISSUER_URL",
    default=DEFAULT_AUTH_ISSUER_URL,
    show_default=True,
    help="OIDC issuer URL (or set INNATE_AUTH_ISSUER_URL). "
    "--token is exchanged for a JWT via this issuer. "
    "Set to empty string to use --token as a plain bearer (dev only).",
)
@click.pass_context
def cli(ctx: click.Context, server: str, token: str, issuer: str) -> None:
    """Innate Training Client — submit and manage training skills & runs."""
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    ctx.obj["token"] = token
    ctx.obj["issuer"] = issuer


# ── Submit ──────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.pass_context
def submit(ctx: click.Context, skill_dir: str) -> None:
    """Create (or reuse) a skill from SKILL_DIR.

    If SKILL_DIR/metadata.json already has a training_skill_id, prints the existing ID.
    Otherwise creates a new skill and writes to metadata.json.
    Use `upload` afterwards to push data files.
    """
    manager = _make_manager(ctx)

    click.echo(f"Registering skill from {skill_dir}")
    gen = manager.submit(skill_dir)
    _print_progress(gen)
    click.echo("Done!")


# ── Upload ──────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.pass_context
def upload(ctx: click.Context, skill_dir: str) -> None:
    """Upload data files to an existing skill."""
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)
    gen = manager.upload_files(skill_id, skill_dir)
    _print_progress(gen)


# ── Status ──────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.argument("run_id", type=int)
@click.pass_context
def status(ctx: click.Context, skill_dir: str, run_id: int) -> None:
    """Show current status of a run."""
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)
    r = manager.run_status(skill_id, run_id)

    click.echo(f"Skill:        {r.skill_id}")
    click.echo(f"Run:          {r.run_id}")
    click.echo(f"Status:       {r.status}")
    if r.daemon_state:
        click.echo(f"Remote state: {r.daemon_state}")
    if r.error_message:
        click.secho(f"Error:        {r.error_message}", fg="red")
    click.echo(f"Created:      {r.created_at}")
    click.echo(f"Updated:      {r.updated_at}")
    if r.started_at:
        click.echo(f"Started:      {r.started_at}")
    if r.finished_at:
        click.echo(f"Finished:     {r.finished_at}")
    if r.instance_ip:
        click.echo(f"Instance IP:  {r.instance_ip}")


# ── Watch ───────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.argument("run_id", type=int)
@click.option("--interval", type=float, default=20.0, help="Poll interval in seconds")
@click.pass_context
def watch(ctx: click.Context, skill_dir: str, run_id: int, interval: float) -> None:
    """Poll run status until completion."""
    ctx.obj["interval"] = interval
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)

    click.echo(
        f"Watching run {skill_id}/{run_id} (poll every {interval}s, Ctrl+C to stop)"
    )
    try:
        gen = manager.watch(skill_id, run_id, interval=interval)
        _print_progress(gen)
    except KeyboardInterrupt:
        click.echo("\nStopped watching.")


# ── Download ────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.argument("run_id", type=int)
@click.option(
    "--dest",
    type=click.Path(),
    default=None,
    help="Destination dir (default: SKILL_DIR)",
)
@click.pass_context
def download(ctx: click.Context, skill_dir: str, run_id: int, dest: str | None) -> None:
    """Download results for a completed run."""
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)
    dest = dest or skill_dir
    gen = manager.download(skill_id, run_id, dest_dir=dest)
    _print_progress(gen)


# ── Fetch Input Data ────────────────────────────────────────────────


@cli.command("fetch-data")
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.option("--dest", type=click.Path(), default=None,
              help="Destination dir (default: SKILL_DIR/data)")
@click.pass_context
def fetch_data(ctx: click.Context, skill_dir: str, dest: str | None) -> None:
    """Download the input training data files for a skill."""
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)
    dest = dest or str(Path(skill_dir) / "data")
    gen = manager.fetch_data(skill_id, dest)
    _print_progress(gen)


# ── List Skills ─────────────────────────────────────────────────────


@cli.command("skills")
@click.pass_context
def list_skills(ctx: click.Context) -> None:
    """List all your skills."""
    manager = _make_manager(ctx)
    skills = manager.list_skills()

    if not skills:
        click.echo("No skills found.")
        return

    click.echo(f"{'SKILL ID':<38} {'NAME':<24} {'CREATED':<20}")
    click.echo("─" * 84)
    for s in skills:
        created = (s.created_at or "")[:19]
        click.echo(f"{s.skill_id:<38} {s.name:<24} {created:<20}")


# ── List Runs ───────────────────────────────────────────────────────


@cli.command("runs")
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.pass_context
def list_runs(ctx: click.Context, skill_dir: str) -> None:
    """List all runs for a skill."""
    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)
    runs = manager.list_runs(skill_id)

    if not runs:
        click.echo("No runs found.")
        return

    click.echo(f"{'RUN ID':<8} {'STATUS':<24} {'CREATED':<20}")
    click.echo("─" * 54)
    for r in runs:
        state = r.status
        if r.daemon_state:
            state += f" ({r.daemon_state})"
        created = (r.created_at or "")[:19]
        click.echo(f"{r.run_id:<8} {state:<24} {created:<20}")


# ── Create Run ──────────────────────────────────────────────────────


@cli.command("run")
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.option(
    "--preset", default=None, help="Server-side preset name (e.g. act-default)"
)
@click.option("--repo", default=None, help="GitHub repo (owner/repo)")
@click.option("--ref", default=None, help="Git ref (branch/tag/commit)")
@click.option("--command", "-c", multiple=True, help="Training command parts")
@click.option("--hours", type=float, default=None, help="Estimated duration (hours)")
@click.option("--gpu-type", default=None, help="GPU type (e.g. H100)")
@click.option("--min-gpus", type=int, default=None)
@click.option("--max-gpus", type=int, default=None)
@click.option(
    "--budget", type=float, default=None, help="Max total cost USD (optional)"
)
@click.option(
    "--checkpoint-patterns",
    "-p",
    multiple=True,
    help="Glob patterns for files to upload (e.g. 'checkpoints/**/*.pt')",
)
@click.option(
    "--env", "-e", multiple=True, help="Environment variables as KEY=VALUE (repeatable)"
)
@click.pass_context
def create_run(
    ctx: click.Context,
    skill_dir: str,
    preset: str | None,
    repo: str | None,
    ref: str | None,
    command: tuple[str, ...],
    hours: float | None,
    gpu_type: str | None,
    min_gpus: int | None,
    max_gpus: int | None,
    budget: float | None,
    checkpoint_patterns: tuple[str, ...],
    env: tuple[str, ...],
) -> None:
    """Create a new run for an existing skill (starts as waiting_for_approval).

    When --preset is given, all training options are optional and the server
    fills in defaults from the named preset.  Explicit flags override preset
    values.
    """
    # Without a preset, the core options are mandatory
    if not preset:
        missing = []
        if not repo:
            missing.append("--repo")
        if not ref:
            missing.append("--ref")
        if not command:
            missing.append("--command / -c")
        if not gpu_type:
            missing.append("--gpu-type")
        if missing:
            raise click.UsageError(
                f"Missing required options (or use --preset): {', '.join(missing)}"
            )

    manager = _make_manager(ctx)
    skill_id = _resolve_skill_id(skill_dir)

    params: dict[str, object] = {}
    if preset:
        params["preset"] = preset
    if repo:
        params["repo"] = repo
    if ref:
        params["ref"] = ref
    if command:
        params["command"] = list(command)
    if hours is not None:
        params["hours"] = hours
    if gpu_type:
        params["gpu_type"] = gpu_type
    if min_gpus is not None:
        params["min_gpus"] = min_gpus
    if max_gpus is not None:
        params["max_gpus"] = max_gpus
    if budget is not None:
        params["budget"] = budget
    if checkpoint_patterns:
        params["checkpoint_patterns"] = list(checkpoint_patterns)
    if env:
        env_dict = {}
        for item in env:
            if "=" not in item:
                raise click.BadParameter(
                    f"Expected KEY=VALUE, got: {item!r}", param_hint="--env"
                )
            k, v = item.split("=", 1)
            env_dict[k] = v
        params["env"] = env_dict

    params["source_dir"] = str(Path(skill_dir).resolve())

    run = manager.client.create_run(skill_id, training_params=params)
    click.echo(f"Run created: {skill_id}/{run['run_id']}")
    click.echo(f"Status: {run['status']}")


# ── Activate ────────────────────────────────────────────────────────


@cli.command()
@click.argument("skill_dir", type=click.Path(exists=True, file_okay=False), default=".")
@click.argument("run_id", type=int)
@click.pass_context
def activate(ctx: click.Context, skill_dir: str, run_id: int) -> None:
    """Activate a trained run: set checkpoint and stats_file in metadata.json.

    Looks inside SKILL_DIR/RUN_ID for the largest *_step_*.pth checkpoint
    and a *stats*.pt file, then writes them into metadata.json's
    execution block.
    """
    manager = _make_manager(ctx)
    try:
        result = manager.activate_run(skill_dir, run_id)
    except (FileNotFoundError, ValueError) as e:
        raise click.UsageError(str(e))

    click.echo(f"Activated run {run_id}:")
    click.echo(f"  checkpoint:  {result['checkpoint']}")
    click.echo(f"  stats_file:  {result['stats_file']}")


# ── Web UI ──────────────────────────────────────────────────────────


@cli.command("ui")
@click.option("--port", type=int, default=8080, help="HTTP port")
@click.option(
    "--skills-dir",
    type=click.Path(exists=True, file_okay=False),
    default=os.path.expanduser("~/skills"),
    help="Root skills directory (default: ~/skills)",
)
@click.pass_context
def launch_ui(ctx: click.Context, port: int, skills_dir: str) -> None:
    """Launch the Training Manager web UI.

    Starts a local web server that lets you browse skills, manage datasets,
    and create training runs from your browser.
    """
    try:
        from training_manager.server import create_app, _get_lan_ip
    except ImportError:
        raise click.UsageError(
            "training-manager package is not installed. "
            "Install it with: pip install -e clients/training-manager"
        )

    import uvicorn

    os.environ.setdefault("TRAINING_SERVER_URL", ctx.obj.get("server", ""))
    os.environ.setdefault("INNATE_SERVICE_KEY", ctx.obj.get("token", ""))
    os.environ.setdefault("INNATE_AUTH_ISSUER_URL", ctx.obj.get("issuer", ""))

    app = create_app(skills_dir=skills_dir)

    host_ip = _get_lan_ip()
    click.echo()
    click.secho("  Training Manager", bold=True)
    click.echo(f"    Local:   http://localhost:{port}")
    if host_ip:
        click.echo(f"    Network: http://{host_ip}:{port}")
    click.echo()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


# ── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
