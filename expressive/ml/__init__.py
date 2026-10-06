"""5090-side ML for MARS expressive motion: retargeting, the plan -> motion generator, the Codex-authored
teacher dataset, the distilled planners and the planner service. Not shipped to the robot."""

import _core  # noqa: F401 — puts brain_client (the expressive core) on sys.path for every ml module
