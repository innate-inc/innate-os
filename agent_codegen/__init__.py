"""
agent_codegen
=============
Self-contained library that generates Python Agent and Skill classes from a
missing-capabilities spec using MiniMax via the Anthropic-compatible API.

Zero dependency on brain_client, ROS2, or innate-os internals.

Public API::

    from agent_codegen import generate_agent, generate_skill
    from agent_codegen import GenerationResult, SkillGenerationResult, AgentCodegenError

    # Generate agent + all its missing skills in one call:
    result = generate_agent(
        missing_capabilities=data["missing_capabilities"],
        api_key=os.environ["MINIMAX_API_KEY"],
        output_path="/path/to/agents/new_agent.py",
        agents_dir="/path/to/agents",
        agent_types_path="/path/to/brain_client/agent_types.py",
        skills_dir="/path/to/skills",
        skill_types_path="/path/to/brain_client/skill_types.py",
    )
    for skill in result.skills:
        print(skill.skill_name, skill.file_path)

    # Or generate a single skill directly:
    skill = generate_skill(
        "greet_visitor",
        "Wave and say hello to detected visitors.",
        api_key=os.environ["MINIMAX_API_KEY"],
        output_path="/path/to/skills/greet_visitor.py",
        skills_dir="/path/to/skills",
        skill_types_path="/path/to/brain_client/skill_types.py",
    )
"""
from agent_codegen.generator import generate_agent, generate_skill
from agent_codegen.models import AgentCodegenError, GenerationResult, SkillGenerationResult

__all__ = [
    "generate_agent",
    "generate_skill",
    "GenerationResult",
    "SkillGenerationResult",
    "AgentCodegenError",
    "run_pipeline",
    "PipelineResult",
]


def __getattr__(name: str):
    # Lazy-load pipeline symbols to avoid a RuntimeWarning when running
    # `python -m agent_codegen.pipeline` (double-import of the submodule).
    if name in ("run_pipeline", "PipelineResult"):
        from agent_codegen.pipeline import run_pipeline, PipelineResult  # noqa: PLC0415
        globals()["run_pipeline"] = run_pipeline
        globals()["PipelineResult"] = PipelineResult
        return globals()[name]
    raise AttributeError(f"module 'agent_codegen' has no attribute {name!r}")
