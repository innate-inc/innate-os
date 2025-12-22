#!/usr/bin/env python3
"""
Brain Client Initializers

This module contains initialization functions for skills and agents
to keep the main brain_client_node.py clean and focused.
"""

import os
import sys
import importlib.util
from typing import Dict, Any, Optional, Tuple

from brain_client.primitive_loader import SkillLoader
from brain_client.directive_loader import AgentLoader


def initialize_skills(logger, simulator_mode: bool = False) -> Dict[str, Any]:
    """
    Initialize all skills using dynamic loading.

    Args:
        logger: ROS logger instance
        simulator_mode: Whether to use simulator navigation skill

    Returns:
        Dictionary mapping skill names to their instances
    """
    skill_loader = SkillLoader(logger)

    # Define directories to scan for primitives
    # Using the unified primitives directory at the root plus ~/skills
    innate_root = os.environ.get(
        "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
    )
    primitives_directories = [
        os.path.join(innate_root, "primitives"),
        os.path.join(os.path.expanduser("~"), "skills"),
    ]

    # Ensure ~/skills directory exists
    os.makedirs(primitives_directories[1], exist_ok=True)

    # Load all skills dynamically from all directories
    discovered_skills = skill_loader.load_skills_from_directories(
        primitives_directories
    )

    logger.info(f"Discovered skills: {list(discovered_skills.keys())}")

    # Handle special case for navigation skill based on simulator mode
    if simulator_mode and "navigate_to_position_sim" in discovered_skills:
        # In simulator mode, use the sim version for navigate_to_position
        logger.info(
            "Simulator mode: using NavigateToPositionSim for navigate_to_position"
        )
        discovered_skills["navigate_to_position"] = discovered_skills[
            "navigate_to_position_sim"
        ]
        # Remove the _sim variant so it doesn't appear as a separate skill
        del discovered_skills["navigate_to_position_sim"]
    elif "navigate_to_position_sim" in discovered_skills:
        # In real robot mode, remove the sim version entirely
        logger.info("Real robot mode: using standard NavigateToPosition")
        del discovered_skills["navigate_to_position_sim"]

    # Create skill instances
    skills_dict = {}
    for skill_name, skill_class in discovered_skills.items():
        try:
            skill_instance = skill_class(logger)
            skills_dict[skill_name] = skill_instance
            logger.debug(f"Loaded skill: {skill_name}")
        except Exception as e:
            logger.error(f"Error instantiating skill {skill_name}: {e}")

    logger.info(f"Successfully loaded {len(skills_dict)} skills")
    return skills_dict


def initialize_agents(
    logger, skills_dict: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """
    Initialize all agents using dynamic loading.

    Args:
        logger: ROS logger instance
        skills_dict: Optional dictionary of available skills for validation

    Returns:
        Tuple of (agents_dict, current_agent) where:
        - agents_dict: Dictionary mapping agent names to their instances
        - current_agent: The default agent instance to use
    """
    agent_loader = AgentLoader(logger)

    # Define directories to scan for directives
    # Using the unified directives directory at the root plus ~/agents
    innate_root = os.environ.get(
        "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
    )
    directives_directories = [
        os.path.join(innate_root, "directives"),
        os.path.join(os.path.expanduser("~"), "agents"),
    ]

    # Ensure ~/agents directory exists
    os.makedirs(directives_directories[1], exist_ok=True)

    directives_directory = directives_directories[0]  # Keep for icon loading

    # Load all agents dynamically from all directories
    discovered_agent_classes = agent_loader.load_agents_from_directories(
        directives_directories
    )

    # Create agent instances with skill validation and icon loading
    agents = agent_loader.create_agent_instances(
        discovered_agent_classes,
        available_skills=skills_dict,
        agents_directory=directives_directory,
    )

    logger.info(f"Successfully loaded {len(agents)} agents")

    # Set default agent (fallback to first available if empty_directive not found)
    current_agent = None
    if "empty_directive" in agents:
        current_agent = agents["empty_directive"]
        logger.debug("Using empty_directive as default")
    elif agents:
        first_agent_name = next(iter(agents))
        current_agent = agents[first_agent_name]
        logger.info(f"Using {first_agent_name} as default agent")
    else:
        logger.error("No agents loaded! This will cause issues.")

    return agents, current_agent
