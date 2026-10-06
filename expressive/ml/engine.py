"""MotionEngine: prompt -> recipe (distilled planner on vLLM) -> plans -> motion (flow generator) -> Clip JSON.

- planners: one vLLM engine per effort tier ("medium" = Qwen3.5-4B, "low" = Qwen3.5-0.8B), the FROZEN system prompt
  prefix-cached; an invalid greedy answer is resampled at T = 0.7 up to ``retries`` times
- serving is async: concurrent requests share each engine's continuous batching (``AsyncPlanner``) and the generator
  batches every request waiting for it into one forward pass on its own thread (``GeneratorBatcher``)
- recipes expand through the core DSL into SERVING plans (keys every 0.25 s, 2 Hz posture) so 0.12 s snaps stay snaps,
  while the generator was trained on 1 Hz extracted plans (Binh's §4.6 split)
- ``Planner`` is the same model offline (synchronous ``vllm.LLM``) for the eval scripts
"""

from __future__ import annotations

import asyncio
import itertools
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from brain_client.expressive.dsl import variants
from brain_client.expressive.motion import Clip
from brain_client.expressive.prompt import messages

from .distill.common import parse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vllm import SamplingParams

    from brain_client.expressive.channels import Frames
    from brain_client.expressive.plan import Plan

    from .generator.sample import Generator

MAX_TOKENS = 600
RETRY_TEMPERATURE = 0.7

Job = tuple[list["Plan"], list[int], "asyncio.Future[list[Frames]]"]


class PlannerError(ValueError):
    pass


def _engine_args(path: str, gpu_memory_utilization: float, fp8: bool, spec_tokens: int) -> dict[str, Any]:
    return {
        "model": path,
        "enable_prefix_caching": True,
        "max_model_len": 2048,
        "dtype": "bfloat16",
        "quantization": "fp8" if fp8 else None,
        "gpu_memory_utilization": gpu_memory_utilization,
        "limit_mm_per_prompt": {"image": 0, "video": 0},
        "max_num_seqs": 16,
        "speculative_config": {"method": "mtp", "num_speculative_tokens": spec_tokens} if spec_tokens else None,
    }


def _sampling(temperature: float, seed: int, max_tokens: int) -> SamplingParams:
    from vllm import SamplingParams

    return SamplingParams(temperature=temperature, top_p=0.95 if temperature else 1.0, max_tokens=max_tokens, seed=seed)


class Planner:
    """One fine-tuned planner on vLLM, offline (bf16 or FP8 weights, optional MTP drafts, prefix-cached prompt)."""

    def __init__(
        self,
        path: str,
        gpu_memory_utilization: float = 0.4,
        fp8: bool = False,
        spec_tokens: int = 0,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        from transformers import AutoTokenizer
        from vllm import LLM

        self.path, self.max_tokens = path, max_tokens
        self.tok = AutoTokenizer.from_pretrained(path)
        self.llm = LLM(**_engine_args(path, gpu_memory_utilization, fp8, spec_tokens))

    def text(self, prompt: str) -> str:
        return self.tok.apply_chat_template(
            messages(prompt), tokenize=False, add_generation_prompt=True, enable_thinking=False
        )

    def complete(self, prompts: Sequence[str], temperature: float, seeds: Sequence[int]) -> list[str]:
        params = [_sampling(temperature, s, self.max_tokens) for s in seeds]
        return [o.outputs[0].text for o in self.llm.generate([self.text(p) for p in prompts], params, use_tqdm=False)]


class AsyncPlanner:
    """The served planner on vLLM's AsyncLLM: requests that arrive together decode together."""

    def __init__(
        self,
        path: str,
        gpu_memory_utilization: float = 0.4,
        fp8: bool = False,
        spec_tokens: int = 0,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        from transformers import AutoTokenizer
        from vllm import AsyncEngineArgs
        from vllm.v1.engine.async_llm import AsyncLLM

        self.path, self.max_tokens = path, max_tokens
        self.tok = AutoTokenizer.from_pretrained(path)
        self.llm = AsyncLLM.from_engine_args(
            AsyncEngineArgs(**_engine_args(path, gpu_memory_utilization, fp8, spec_tokens))
        )
        self._ids = itertools.count()

    async def complete(self, prompt: str, temperature: float, seed: int) -> str:
        text = self.tok.apply_chat_template(
            messages(prompt), tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        final = ""
        params = _sampling(temperature, seed, self.max_tokens)
        async for out in self.llm.generate(text, params, request_id=f"plan-{next(self._ids)}"):
            final = out.outputs[0].text
        return final

    async def plan(self, prompt: str, seed: int = 0, retries: int = 2) -> tuple[str, str]:
        """Greedy recipe; if it is invalid, up to ``retries`` samples at ``RETRY_TEMPERATURE``."""
        idea, recipe = parse(await self.complete(prompt, 0.0, seed))
        for k in range(retries):
            if recipe:
                break
            idea, recipe = parse(await self.complete(prompt, RETRY_TEMPERATURE, seed + k + 1))
        if recipe is None:
            raise PlannerError(f"planner wrote no valid recipe for {prompt!r} in {retries + 1} tries")
        return idea, recipe


class GeneratorBatcher:
    """The flow generator on one worker thread; every request that queues while a batch runs joins the next one."""

    def __init__(self, generator: Generator, steps: int, cfg: float) -> None:
        self.generator, self.steps, self.cfg = generator, steps, cfg
        self._thread = ThreadPoolExecutor(1, thread_name_prefix="generator")
        self._queue: asyncio.Queue[Job] | None = None
        self._worker: asyncio.Task[None] | None = None

    async def generate(self, plans: list[Plan], seeds: list[int]) -> list[Frames]:
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._worker = asyncio.get_running_loop().create_task(self._run(self._queue))
        done: asyncio.Future[list[Frames]] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((plans, seeds, done))
        return await done

    async def _run(self, queue: asyncio.Queue[Job]) -> None:
        loop = asyncio.get_running_loop()
        while True:
            jobs = [await queue.get()]
            while not queue.empty():
                jobs.append(queue.get_nowait())
            plans = [p for job in jobs for p in job[0]]
            seeds = [s for job in jobs for s in job[1]]
            try:
                motions = await loop.run_in_executor(self._thread, self._forward, plans, seeds)
            except Exception as e:  # noqa: BLE001 — a failed batch fails its requests, never the batcher
                for _, _, done in jobs:
                    if not done.done():
                        done.set_exception(e)
                continue
            start = 0
            for job_plans, _, done in jobs:
                if not done.done():  # a client that hung up cancelled its future
                    done.set_result(motions[start : start + len(job_plans)])
                start += len(job_plans)

    def _forward(self, plans: list[Plan], seeds: list[int]) -> list[Frames]:
        return self.generator.generate_batch(plans, seeds=seeds, steps=self.steps, cfg=self.cfg)


class MotionEngine:
    def __init__(
        self, planners: dict[str, AsyncPlanner], generator: Generator, steps: int = 8, cfg: float = 1.5
    ) -> None:
        self.planners, self.generator, self.steps, self.cfg = planners, generator, steps, cfg
        self.default = "medium" if "medium" in planners else next(iter(planners))
        self.batcher = GeneratorBatcher(generator, steps, cfg)

    async def warm_up(self) -> None:
        """Compile what the first requests would hit: a dense request, a batch of concurrent decodes, a sampled retry."""
        for effort, planner in self.planners.items():
            await self.dense("warm-up. You stretch after waking.", n=1, effort=effort)
            await asyncio.gather(*(planner.complete(f'You say: "Warming up, step {k}."', 0.0, k) for k in range(8)))
            await planner.complete("warm-up. You stretch after waking.", RETRY_TEMPERATURE, 1)

    def shutdown(self) -> None:
        for planner in self.planners.values():
            planner.llm.shutdown()

    async def _plan(
        self, prompt: str, n: int, seed: int, effort: str | None, retries: int
    ) -> tuple[str, str, str, list[Plan], int]:
        effort = effort or self.default
        if effort not in self.planners:
            raise PlannerError(f"effort {effort!r} not loaded; available: {sorted(self.planners)}")
        start = time.perf_counter()
        idea, recipe = await self.planners[effort].plan(prompt, seed, retries)
        plans = variants(recipe, n, seed=seed)
        return effort, idea, recipe, plans, round((time.perf_counter() - start) * 1e3)

    async def sparse(
        self, prompt: str, n: int = 1, seed: int = 0, effort: str | None = None, retries: int = 2
    ) -> dict[str, Any]:
        """Planner only: the recipe and its n keyframe plans (the same seed gives the plans ``dense`` uses)."""
        effort, idea, recipe, plans, planner_ms = await self._plan(prompt, n, seed, effort, retries)
        return {
            "prompt": prompt,
            "effort": effort,
            "idea": idea,
            "recipe": recipe,
            "plans": plans,
            "timing_ms": {"planner": planner_ms, "total": planner_ms},
        }

    async def dense(
        self, prompt: str, n: int = 1, seed: int = 0, effort: str | None = None, retries: int = 2
    ) -> dict[str, Any]:
        """Full pipeline: plans -> 25 Hz plan-space motion -> Clip JSON (CONTRACTS §4), one per variant."""
        effort, idea, recipe, plans, planner_ms = await self._plan(prompt, n, seed, effort, retries)
        start = time.perf_counter()
        motions = await self.batcher.generate(plans, [seed * 1000 + i for i in range(n)])
        generator_ms = round((time.perf_counter() - start) * 1e3)
        clips = [
            Clip.from_plan_frames(
                m, name=f"{prompt.split('.')[0][:40]}#{i}", prompt=prompt, idea=idea, recipe=recipe
            ).to_dict()
            for i, m in enumerate(motions)
        ]
        return {
            "prompt": prompt,
            "effort": effort,
            "idea": idea,
            "recipe": recipe,
            "plans": plans,
            "clips": clips,
            "timing_ms": {"planner": planner_ms, "generator": generator_ms, "total": planner_ms + generator_ms},
        }

    def health(self) -> dict[str, Any]:
        return {
            "planners": {e: p.path for e, p in self.planners.items()},
            "generator": {
                "step": self.generator.step,
                "device": self.generator.dev,
                "steps": self.steps,
                "cfg": self.cfg,
            },
        }
