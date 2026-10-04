"""mars-express: plan, render, probe and maintain MARS expressive motion from the host.

uv run mars-express plan "<prompt>" [--chat gemini|openai|preset] [--model M] [--seed N] [--out clip.json]
uv run mars-express render clip.json out.mp4 [--camera three-quarter|front|profile|split] [--env void]
uv run mars-express probe recipes.jsonl          # lines {"prompt": ..., "recipe": ...}; --teacher for the reference
uv run mars-express sheet [--out out/]           # axis sheets: person's eyes, three-quarter, head camera
uv run mars-express presets [--out out/presets]  # every built-in preset as mp4 + contact sheet
uv run mars-express basis                        # rebuild basis.json's safe table, report collisions
uv run mars-express golden                       # rewrite fixtures/golden.json (port checks)
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

import _core  # noqa: F401

OUT = Path(__file__).with_name("out")


def _plan(args: argparse.Namespace) -> None:
    from brain_client.expressive import planner, presets
    from brain_client.expressive.motion import Clip

    start = time.monotonic()
    if args.chat == "preset":
        name = presets.match(args.prompt)
        preset = presets.PRESETS[name]
        print(f"preset {name}: {preset.idea}")
        clip = presets.clip(name, seed=args.seed, prompt=args.prompt)
    else:
        from chat import endpoint_chat

        written = planner.write(args.prompt, endpoint_chat(args.chat, args.model), retries=args.retries)
        print(f"idea:   {written.idea}\nrecipe: {written.recipe}\n({written.attempts} attempt(s))")
        clip = Clip.from_recipe(written.recipe, name=args.prompt, seed=args.seed, prompt=args.prompt, idea=written.idea)
    out = clip.save(args.out or OUT / f"{args.prompt.split('.')[0].strip().replace(' ', '_')[:40]}.json")
    print(f"{clip.duration:.2f} s clip -> {out}  ({time.monotonic() - start:.2f} s)")


def _render(args: argparse.Namespace) -> None:
    from render.clip import render_clip
    from render.sheets import contact_sheet

    from brain_client.expressive.motion import Clip

    clip = Clip.load(args.clip)
    out = render_clip(clip, args.out, env=args.env, camera=args.camera)
    print(out, contact_sheet(clip, Path(args.out).with_suffix(".png")))


def _probe(args: argparse.Namespace) -> None:
    from brain_client.expressive import probes

    if args.teacher:
        recipes = {prompt: [recipe] for prompt, recipe in probes.TEACHER.items()}
    else:
        recipes: dict[str, list[str | None]] = defaultdict(list)
        for line in Path(args.recipes).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                recipes[row["prompt"]].append(row.get("recipe"))
    total, per = probes.score(recipes)
    for probe in probes.PROBES:
        print(f"{per[probe.prompt]:5.0%}  {probe.prompt[:60]:60s}  {probe.expects}")
    ood, skill = probes.split(per)
    print(f"overall {total:.0%}   ood-core {ood:.0%}   skill {skill:.0%}")


def _sheet(args: argparse.Namespace) -> None:
    from render.sheets import axis_sheet
    from render.stage import Stage

    from brain_client.expressive.basis import Basis

    stage = Stage(size=(440, 400))
    basis = Basis.load()
    views = (
        ("front", "axis_sheet_front.png"),
        ("three-quarter", "axis_sheet_3q.png"),
        ("main", "axis_sheet_maincam.png"),
    )
    for view, name in views:
        print(axis_sheet(basis, Path(args.out) / name, view, stage))


def _presets(args: argparse.Namespace) -> None:
    from render.clip import render_clip
    from render.sheets import contact_sheet

    from brain_client.expressive import presets

    out = Path(args.out)
    for name in args.names or presets.PRESETS:
        clip = presets.clip(name)
        clip.save(out / f"{name}.json")
        print(render_clip(clip, out / f"{name}.mp4", camera=args.camera), contact_sheet(clip, out / f"{name}.png"))


def _basis(_: argparse.Namespace) -> None:
    import basis_tool

    print(json.dumps(basis_tool.rebuild(), indent=2))


def _golden(_: argparse.Namespace) -> None:
    import golden

    print(golden.write())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mars-express", description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="prompt -> recipe -> clip json")
    plan.add_argument("prompt")
    plan.add_argument("--chat", choices=("gemini", "openai", "preset"), default="preset")
    plan.add_argument("--model")
    plan.add_argument("--seed", type=int, default=0)
    plan.add_argument("--retries", type=int, default=2)
    plan.add_argument("--out")
    plan.set_defaults(run=_plan)

    render = commands.add_parser("render", help="clip json -> mp4 (+ contact sheet png)")
    render.add_argument("clip")
    render.add_argument("out")
    render.add_argument("--camera", choices=("front", "three-quarter", "profile", "split"), default="three-quarter")
    render.add_argument("--env", default="void")
    render.set_defaults(run=_render)

    probe = commands.add_parser("probe", help="score recipes on the physical probes")
    probe.add_argument("recipes", nargs="?")
    probe.add_argument("--teacher", action="store_true", help="score probes.TEACHER")
    probe.set_defaults(run=_probe)

    sheet = commands.add_parser("sheet", help="render the basis axis sheets")
    sheet.add_argument("--out", default=str(OUT))
    sheet.set_defaults(run=_sheet)

    preset_cmd = commands.add_parser("presets", help="render the built-in presets")
    preset_cmd.add_argument("names", nargs="*")
    preset_cmd.add_argument("--out", default=str(OUT / "presets"))
    preset_cmd.add_argument("--camera", choices=("front", "three-quarter", "profile", "split"), default="three-quarter")
    preset_cmd.set_defaults(run=_presets)

    commands.add_parser("basis", help="rebuild the safe table in basis.json").set_defaults(run=_basis)
    commands.add_parser("golden", help="rewrite fixtures/golden.json").set_defaults(run=_golden)
    args = parser.parse_args(argv)
    if args.command == "probe" and not (args.recipes or args.teacher):
        parser.error("probe needs recipes.jsonl or --teacher")
    args.run(args)


if __name__ == "__main__":
    main()
