#!/usr/bin/env python3
"""
Brain Client Initializers

This module contains initialization functions for skills and agents
to keep the main brain_client_node.py clean and focused.
"""

import os
from typing import Dict, Any, Optional, Tuple

from brain_client.agent_loader import AgentLoader


def initialize_agents(
    logger, skills_dict: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """
    Initialize all agents using dynamic loading.

    Args:
        logger: ROS logger instance
        skills_dict: Optional dictionary of available skills for validation

    Returns:
        Tuple of (agents_dict, default_agent) where:
        - agents_dict: Dictionary mapping agent names to their instances
        - default_agent: The default agent instance to use
    """
    agent_loader = AgentLoader(logger)

    # Define directories to scan for agents
    # Using the unified agents directory at the root plus ~/agents
    innate_root = os.environ.get(
        "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
    )
    agents_directories = [
        os.path.join(innate_root, "agents"),
        os.path.join(os.path.expanduser("~"), "agents"),
    ]

    # Ensure ~/agents directory exists
    os.makedirs(agents_directories[1], exist_ok=True)

    agents_directory = agents_directories[0]  # Keep for icon loading

    # Load all agents dynamically from all directories
    discovered_agent_classes = agent_loader.load_agents_from_directories(
        agents_directories
    )

    # Create agent instances with skill validation and icon loading
    agents = agent_loader.create_agent_instances(
        discovered_agent_classes,
        available_skills=skills_dict,
        agents_directory=agents_directory,
    )

    logger.info(f"Successfully loaded {len(agents)} agents")

    # Default directive: orchestrator first, then legacy empty_directive, else first loaded.
    # Note: This doesn't mean the agent runs — is_brain_active controls that.
    default_agent = None
    if "orchestrator_agent" in agents:
        default_agent = agents["orchestrator_agent"]
        logger.debug("Using orchestrator_agent as default")
    elif "empty_directive" in agents:
        default_agent = agents["empty_directive"]
        logger.debug("Using empty_directive as default")
    elif agents:
        first_agent_name = next(iter(agents))
        default_agent = agents[first_agent_name]
        logger.debug(f"Using {first_agent_name} as default agent")
    else:
        logger.error("No agents loaded! This will cause issues.")

    return agents, default_agent
