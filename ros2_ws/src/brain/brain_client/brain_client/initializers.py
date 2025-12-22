#!/usr/bin/env python3
"""
Brain Client Initializers

This module contains initialization functions for primitives and directives
to keep the main brain_client_node.py clean and focused.
"""

import os
import sys
import importlib.util
from typing import Dict, Any, Optional, Tuple

from brain_client.primitive_loader import PrimitiveLoader
from brain_client.directive_loader import DirectiveLoader


def initialize_primitives(logger, simulator_mode: bool = False) -> Dict[str, Any]:
    """
    Initialize all primitives using dynamic loading.

    Args:
        logger: ROS logger instance
        simulator_mode: Whether to use simulator navigation primitive

    Returns:
        Dictionary mapping primitive names to their instances
    """
    primitive_loader = PrimitiveLoader(logger)

    # Define directories to scan for primitives
    # Using the unified primitives directory at the root plus ~/skills
    maurice_root = os.environ.get(
        "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
    )
    primitives_directories = [
        os.path.join(maurice_root, "primitives"),
        os.path.join(os.path.expanduser("~"), "skills"),
    ]

    # Load all primitives dynamically from all directories
    discovered_primitives = primitive_loader.load_primitives_from_directories(
        primitives_directories
    )

    logger.info(f"Discovered primitives: {list(discovered_primitives.keys())}")

    # Handle special case for navigation primitive based on simulator mode
    if simulator_mode and "navigate_to_position_sim" in discovered_primitives:
        # In simulator mode, use the sim version for navigate_to_position
        logger.info(
            "Simulator mode: using NavigateToPositionSim for navigate_to_position"
        )
        discovered_primitives["navigate_to_position"] = discovered_primitives[
            "navigate_to_position_sim"
        ]
        # Remove the _sim variant so it doesn't appear as a separate primitive
        del discovered_primitives["navigate_to_position_sim"]
    elif "navigate_to_position_sim" in discovered_primitives:
        # In real robot mode, remove the sim version entirely
        logger.info("Real robot mode: using standard NavigateToPosition")
        del discovered_primitives["navigate_to_position_sim"]

    # Create primitive instances
    primitives_dict = {}
    for primitive_name, primitive_class in discovered_primitives.items():
        try:
            primitive_instance = primitive_class(logger)
            primitives_dict[primitive_name] = primitive_instance
            logger.debug(f"Loaded primitive: {primitive_name}")
        except Exception as e:
            logger.error(f"Error instantiating primitive {primitive_name}: {e}")

    logger.info(f"Successfully loaded {len(primitives_dict)} primitives")
    return primitives_dict


def initialize_directives(
    logger, primitives_dict: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """
    Initialize all directives using dynamic loading.

    Args:
        logger: ROS logger instance
        primitives_dict: Optional dictionary of available primitives for validation

    Returns:
        Tuple of (directives_dict, current_directive) where:
        - directives_dict: Dictionary mapping directive names to their instances
        - current_directive: The default directive instance to use
    """
    directive_loader = DirectiveLoader(logger)

    # Define directories to scan for directives
    # Using the unified directives directory at the root plus ~/agents
    maurice_root = os.environ.get(
        "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
    )
    directives_directories = [
        os.path.join(maurice_root, "directives"),
        os.path.join(os.path.expanduser("~"), "agents"),
    ]
    directives_directory = directives_directories[0]  # Keep for icon loading

    # Load all directives dynamically from all directories
    discovered_directive_classes = directive_loader.load_directives_from_directories(
        directives_directories
    )

    # Create directive instances with primitive validation and icon loading
    directives = directive_loader.create_directive_instances(
        discovered_directive_classes,
        available_primitives=primitives_dict,
        directives_directory=directives_directory,
    )

    logger.info(f"Successfully loaded {len(directives)} directives")

    # Set default directive (fallback to first available if empty_directive not found)
    current_directive = None
    if "empty_directive" in directives:
        current_directive = directives["empty_directive"]
        logger.debug("Using empty_directive as default")
    elif directives:
        first_directive_name = next(iter(directives))
        current_directive = directives[first_directive_name]
        logger.info(f"Using {first_directive_name} as default directive")
    else:
        logger.error("No directives loaded! This will cause issues.")

    return directives, current_directive
