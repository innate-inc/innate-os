"""
agent_codegen
=============
Self-contained library that generates Python Agent classes from a
missing-capabilities spec using MiniMax via the Anthropic-compatible API.

Zero dependency on brain_client, ROS2, or innate-os internals.

Public API::

    from agent_codegen import generate_agent, GenerationResult, AgentCodegenError

    result = generate_agent(
        missing_capabilities=data["missing_capabilities"],
        api_key=os.environ["MINIMAX_API_KEY"],
        output_path="/path/to/agents/new_agent.py",
        agents_dir="/path/to/agents",
        agent_types_path="/path/to/brain_client/agent_types.py",
    )
    print(result.code)
"""
from agent_codegen.generator import generate_agent
from agent_codegen.models import AgentCodegenError, GenerationResult

__all__ = ["generate_agent", "GenerationResult", "AgentCodegenError"]
