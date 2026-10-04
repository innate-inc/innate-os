"""MotionEngine: prompt -> recipe (distilled planner on vLLM) -> plans -> motion (flow generator) -> Clip JSON.

- planners: one vLLM engine per effort tier ("medium" = Qwen3.5-4B, "low" = Qwen3.5-0.8B), the FROZEN system prompt
  prefix-cached; an invalid greedy answer is resampled at T = 0.7 up to ``retries`` times
- recipes expand through the core DSL into SERVING plans (keys every 0.25 s, 2 Hz posture) so 0.12 s snaps stay snaps,
  while the generator was trained on 1 Hz extracted plans (Binh's §4.6 split)
- the generator runs all n samples and both guidance branches in one batched forward per Euler step
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from brain_client.expressive.dsl import variants
from brain_client.expressive.motion import Clip
from brain_client.expressive.prompt import messages

from .distill.common import parse
from .generator.sample import Generator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.expressive.plan import Plan

MAX_TOKENS = 600


class PlannerError(ValueError):
    pass


class Planner:
    """One fine-tuned planner on vLLM (bf16 or FP8 weights, optional MTP drafts, prefix cache for the system prompt)."""

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
        self.llm = LLM(
            model=path,
            enable_prefix_caching=True,
            max_model_len=2048,
            dtype="bfloat16",
            quantization="fp8" if fp8 else None,
            gpu_memory_utilization=gpu_memory_utilization,
            limit_mm_per_prompt={"image": 0, "video": 0},
            max_num_seqs=16,
            speculative_config={"method": "mtp", "num_speculative_tokens": spec_tokens} if spec_tokens else None,
        )

    def text(self, prompt: str) -> str:
        return self.tok.apply_chat_template(
            messages(prompt), tokenize=False, add_generation_prompt=True, enable_thinking=False
        )

    def complete(self, prompts: Sequence[str], temperature: float, seeds: Sequence[int]) -> list[str]:
        from vllm import SamplingParams

        params = [
            SamplingParams(
                temperature=temperature, top_p=0.95 if temperature else 1.0, max_tokens=self.max_tokens, seed=s
            )
            for s in seeds
        ]
        return [o.outputs[0].text for o in self.llm.generate([self.text(p) for p in prompts], params, use_tqdm=False)]

    def plan(self, prompt: str, seed: int = 0, retries: int = 2) -> tuple[str, str]:
        """Greedy recipe; if it is invalid, up to ``retries`` samples at temperature 0.7."""
        idea, recipe = parse(self.complete([prompt], 0.0, [seed])[0])
        for k in range(retries):
            if recipe:
                break
            idea, recipe = parse(self.complete([prompt], 0.7, [seed + k + 1])[0])
        if recipe is None:
            raise PlannerError(f"planner wrote no valid recipe for {prompt!r} in {retries + 1} tries")
        return idea, recipe


class MotionEngine:
    def __init__(self, planners: dict[str, Planner], generator: Generator, steps: int = 8, cfg: float = 1.5) -> None:
        self.planners, self.generator, self.steps, self.cfg = planners, generator, steps, cfg
        self.default = "medium" if "medium" in planners else next(iter(planners))
        self.lock = threading.Lock()
        for effort in planners:
            self.dense("warm-up. You stretch after waking.", n=1, effort=effort)

    def _plan(
        self, prompt: str, n: int, seed: int, effort: str | None, retries: int
    ) -> tuple[str, str, str, list[Plan], int]:
        effort = effort or self.default
        if effort not in self.planners:
            raise PlannerError(f"effort {effort!r} not loaded; available: {sorted(self.planners)}")
        start = time.perf_counter()
        idea, recipe = self.planners[effort].plan(prompt, seed, retries)
        plans = variants(recipe, n, seed=seed)
        return effort, idea, recipe, plans, round((time.perf_counter() - start) * 1e3)

    def sparse(
        self, prompt: str, n: int = 1, seed: int = 0, effort: str | None = None, retries: int = 2
    ) -> dict[str, Any]:
        """Planner only: the recipe and its n keyframe plans (the same seed gives the plans ``dense`` uses)."""
        with self.lock:
            effort, idea, recipe, plans, planner_ms = self._plan(prompt, n, seed, effort, retries)
        return {
            "prompt": prompt,
            "effort": effort,
            "idea": idea,
            "recipe": recipe,
            "plans": plans,
            "timing_ms": {"planner": planner_ms, "total": planner_ms},
        }

    def dense(
        self, prompt: str, n: int = 1, seed: int = 0, effort: str | None = None, retries: int = 2
    ) -> dict[str, Any]:
        """Full pipeline: plans -> 25 Hz plan-space motion -> Clip JSON (CONTRACTS §4), one per variant."""
        with self.lock:
            effort, idea, recipe, plans, planner_ms = self._plan(prompt, n, seed, effort, retries)
            start = time.perf_counter()
            motions = self.generator.generate_batch(
                plans, seeds=[seed * 1000 + i for i in range(n)], steps=self.steps, cfg=self.cfg
            )
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
