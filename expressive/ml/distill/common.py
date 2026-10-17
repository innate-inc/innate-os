"""Shared by the distillation scripts and the server: the chat format, answer parsing, model-class choice and the
plan descriptors used to compare a student's recipes with the teacher's."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from brain_client.expressive.channels import FPS, Ch, Frames
from brain_client.expressive.dsl import check, expand
from brain_client.expressive.planner import parse_reply

if TYPE_CHECKING:
    from transformers import AutoModelForCausalLM, AutoModelForImageTextToText


def parse(text: str) -> tuple[str, str | None]:
    """Model output -> (idea, recipe or None when missing or invalid)."""
    try:
        idea, recipe = parse_reply(text.split("</think>")[-1])
    except ValueError:
        return "", None
    return (idea, recipe) if recipe and check(recipe) is None else (idea, None)


def lm_class(path: str) -> type[AutoModelForCausalLM] | type[AutoModelForImageTextToText]:
    """The transformers class a checkpoint was saved as. Qwen3.5 is multimodal (``...ForConditionalGeneration``, text
    weights under ``model.language_model.*``): opened as a text-only CausalLM every key misses and is silently
    re-initialised, and a LoRA merged into it silently applies to nothing."""
    from transformers import AutoConfig, AutoModelForCausalLM, AutoModelForImageTextToText

    arch = (AutoConfig.from_pretrained(path).architectures or [""])[0]
    return AutoModelForImageTextToText if "ConditionalGeneration" in arch else AutoModelForCausalLM


DESCRIPTORS = (
    "duration",
    "approach_mean",
    "expand_mean",
    "expand_min",
    "expand_max",
    "rise_mean",
    "rise_min",
    "rise_max",
    "attend_mean",
    "attend_min",
    "attend_max",
    "askew_range",
    "orient_range",
    "advance_range",
    "grip_max",
    "energy_mean",
    "energy_max",
)


def descriptors(recipe: str) -> Frames:
    """Exact (unjittered) expansion -> the vector named by ``DESCRIPTORS``."""
    f = expand(recipe)
    return np.array(
        [
            (len(f) - 1) / FPS,
            f[:, Ch.APPROACH].mean(),
            f[:, Ch.EXPAND].mean(),
            f[:, Ch.EXPAND].min(),
            f[:, Ch.EXPAND].max(),
            f[:, Ch.RISE].mean(),
            f[:, Ch.RISE].min(),
            f[:, Ch.RISE].max(),
            f[:, Ch.ATTEND].mean(),
            f[:, Ch.ATTEND].min(),
            f[:, Ch.ATTEND].max(),
            np.ptp(f[:, Ch.ASKEW]),
            np.ptp(f[:, Ch.ORIENT]),
            np.ptp(f[:, Ch.ADVANCE]),
            f[:, Ch.GRIP].max(),
            f[:, Ch.ENERGY].mean(),
            f[:, Ch.ENERGY].max(),
        ]
    )


def agreement(a: list[Frames], b: list[Frames]) -> list[float]:
    """Per-descriptor Pearson r between two aligned lists of descriptor vectors (nan where one side is constant)."""
    if len(a) < 2:
        return [float("nan")] * len(DESCRIPTORS)
    x, y = np.array(a), np.array(b)
    return [
        float(np.corrcoef(x[:, j], y[:, j])[0, 1]) if x[:, j].std() > 1e-9 and y[:, j].std() > 1e-9 else float("nan")
        for j in range(x.shape[1])
    ]
