"""mars-express: plan, render, probe and maintain MARS expressive motion from the host.

uv run mars-express plan "<prompt>" [--chat gemini|openai|preset] [--model M] [--seed N] [--out clip.json]
uv run mars-express render clip.json out.mp4 [--camera three-quarter|front|profile|split] [--env void]
uv run mars-express probe recipes.jsonl          # lines {"prompt": ..., "recipe": ...}; --teacher for the reference
uv run mars-express sheet [--out out/]           # axis sheets: person's eyes, three-quarter, head camera
uv run mars-express presets [--out out/presets]  # every built-in preset as mp4 + contact sheet
uv run mars-express basis                        # rebuild basis.json's safe table, report collisions
uv run mars-express golden                       # rewrite fixtures/golden.json (port checks)
uv run mars-express eval [--judge gemini|openai] [--n 3]   # blind recognition eval -> out/eval/REPORT.md
uv run mars-express interaction chat.mp4         # Gemini rates a recorded conversation, reply by reply
uv run mars-express show demo/show.yaml out.mp4  # MARS speaks and emotes through one offline Animator
uv run mars-express demo [--out out/demo]        # mars_explains.mp4, idle_speech.mp4, presets_montage.mp4
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


def _eval(args: argparse.Namespace) -> None:
    import logging

    from eval.arms import ARMS, flow, load_arm
    from eval.run import Config, run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    arms = dict(ARMS) | dict(load_arm(spec) for spec in args.arm)
    if args.flow:
        arms["flow"] = flow(args.flow)
    if args.arms:
        chosen = args.arms.split(",")
        if not set(chosen) <= set(arms):
            raise SystemExit(f"--arms wants some of {sorted(arms)}, got {args.arms!r}")
        arms = {name: arms[name] for name in chosen}
    default_pairs = ["lively,direct"] if {"lively", "direct"} <= set(arms) else []
    pairs = [tuple(spec.split(",")) for spec in args.pair or default_pairs]
    for pair in pairs:
        if len(pair) != 2 or not set(pair) <= set(arms):
            raise SystemExit(f"--pair wants two of {sorted(arms)}, got {','.join(pair)!r}")
    cfg = Config(
        out=Path(args.out),
        judge=args.judge,
        judge_model=args.judge_model,
        n=args.n,
        planner_model=args.planner_model,
        samples=args.samples,
        arms=arms,
        pairs=tuple((pair[0], pair[1]) for pair in pairs),
        workers=args.workers,
        probes=not args.no_probes,
        camera=args.camera,
        audio=args.audio,
        only=tuple(args.only),
    )
    print(run(cfg, report_only=args.report_only, snapshot=Path(args.snapshot) if args.snapshot else None))


def _interaction(args: argparse.Namespace) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from eval.interaction import rate, summary
    from eval.llm import Proxy, load_env

    load_env()
    proxy = Proxy()
    with ThreadPoolExecutor(args.n) as pool:
        runs = list(pool.map(lambda _: rate(Path(args.video), args.model, proxy), range(args.n)))
    out = Path(args.out or Path(args.video).with_suffix(".ratings.json"))
    out.write_text(json.dumps(runs, indent=1))
    print(json.dumps(summary(runs)), out)


def _show(args: argparse.Namespace) -> None:
    import logging

    from demo.show import Show, Studio, render_show

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    out = Path(args.out)
    studio = Studio(out.parent / "cache", None if args.no_planner else args.planner_model)
    print(render_show(Show.load(Path(args.show)), out, studio))


def _demo(args: argparse.Namespace) -> None:
    import logging

    from demo.montage import NAMES, render_montage
    from demo.show import Show, Studio, render_show

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    out, here = Path(args.out), Path(__file__).with_name("demo")
    studio = Studio(out / "cache", None if args.no_planner else args.planner_model)
    print(render_show(Show.load(here / "show.yaml"), out / "mars_explains.mp4", studio))
    print(render_show(Show.load(here / "idle_speech.yaml"), out / "idle_speech.mp4", studio))
    names = tuple(args.montage.split(",")) if args.montage else NAMES
    print(render_montage(out / "presets_montage.mp4", names))


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

    evaluate = commands.add_parser("eval", help="blind recognition eval: plan, film, judge, report")
    evaluate.add_argument("--judge", choices=("gemini", "openai"), default="gemini", help="gemini watches the video")
    evaluate.add_argument("--judge-model", help="default gemini-3.1-pro-preview / gpt-5.5")
    evaluate.add_argument("--n", type=int, default=3, help="independent judge calls per clip and per A/B pair")
    evaluate.add_argument("--planner-model", default="gpt-6-astra")
    evaluate.add_argument("--samples", type=int, default=8, help="planner samples per physical probe")
    evaluate.add_argument("--arm", action="append", default=[], help="extra arm: name=module:function")
    evaluate.add_argument("--flow", metavar="CKPT", help="add the flow generator arm (uv run --extra flow)")
    evaluate.add_argument(
        "--pair", action="append", help="two arms to A/B, e.g. flow,lively (repeatable; default lively,direct)"
    )
    evaluate.add_argument("--arms", help="comma-separated subset of the arms to build and judge, e.g. lively,flow")
    evaluate.add_argument(
        "--camera", choices=("three-quarter", "human"), default="three-quarter", help="human: eye at head height"
    )
    evaluate.add_argument("--only", action="append", default=[], help="item ids to run (fnmatch), e.g. 'preset-*'")
    evaluate.add_argument("--audio", action="store_true", help="mux the robot's vocalization into the videos (gemini)")
    evaluate.add_argument("--out", default=str(OUT / "eval"))
    evaluate.add_argument("--workers", type=int, default=8)
    evaluate.add_argument("--no-probes", action="store_true")
    evaluate.add_argument("--report-only", action="store_true", help="rebuild REPORT.md from the cache")
    evaluate.add_argument("--snapshot", help="also write a committable REPORT.md + JPEG figures here")
    evaluate.set_defaults(run=_eval)

    interaction = commands.add_parser("interaction", help="rate a recorded conversation (mp4 with the voice)")
    interaction.add_argument("video")
    interaction.add_argument("--n", type=int, default=3, help="independent viewings")
    interaction.add_argument("--model", default="gemini-3.1-pro-preview")
    interaction.add_argument("--out", help="ratings json (default: next to the video)")
    interaction.set_defaults(run=_interaction)

    show = commands.add_parser("show", help="render a show.yaml: speech, emotes, idle, subtitles")
    show.add_argument("show")
    show.add_argument("out")
    show.add_argument("--planner-model", default="gpt-6-astra")
    show.add_argument("--no-planner", action="store_true", help="emotes fall back to the closest preset")
    show.set_defaults(run=_show)

    demo = commands.add_parser("demo", help="the three demo videos for the docs")
    demo.add_argument("--out", default=str(OUT / "demo"))
    demo.add_argument("--planner-model", default="gpt-6-astra")
    demo.add_argument("--no-planner", action="store_true")
    demo.add_argument("--montage", help="six comma-separated presets for the montage (default demo.montage.NAMES)")
    demo.set_defaults(run=_demo)

    commands.add_parser("basis", help="rebuild the safe table in basis.json").set_defaults(run=_basis)
    commands.add_parser("golden", help="rewrite fixtures/golden.json").set_defaults(run=_golden)
    args = parser.parse_args(argv)
    if args.command == "probe" and not (args.recipes or args.teacher):
        parser.error("probe needs recipes.jsonl or --teacher")
    args.run(args)


if __name__ == "__main__":
    main()
