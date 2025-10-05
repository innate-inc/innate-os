#!/usr/bin/env python3
"""
Brain Client Initializers

This module contains initialization functions for primitives and directives
to keep the main brain_client_node.py clean and focused.
"""

import os
from typing import Dict, Any, Optional, Tuple

from brain_client.primitive_loader import PrimitiveLoader
from brain_client.directive_loader import DirectiveLoader
from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.navigate_to_position_sim import NavigateToPositionSim


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
    
    # Define directory to scan for primitives
    # Using the new unified primitives directory at the root of maurice-prod
    primitives_directory = os.path.expanduser("~/maurice-prod/primitives")
    
    # Load all primitives dynamically
    discovered_primitives = primitive_loader.discover_primitives_in_directory(primitives_directory)

    print(f"Discovered primitives (BRAIN): {list(discovered_primitives.keys())}")
    
    # Handle special case for navigation primitive based on simulator mode
    navigation_primitive = (
        NavigateToPositionSim if simulator_mode else NavigateToPosition
    )
    
    # Override the navigation primitive if it was discovered
    if "navigate_to_position" in discovered_primitives:
        discovered_primitives["navigate_to_position"] = navigation_primitive
    
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


def initialize_directives(logger, primitives_dict: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Optional[Any]]:
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
    
    # Define directory to scan for directives
    base_dir = os.path.dirname(os.path.abspath(__file__))
    directives_directory = os.path.join(base_dir, "directives")
    
    # Load all directives dynamically
    discovered_directive_classes = directive_loader.discover_directives_in_directory(directives_directory)
    
    # Create directive instances with primitive validation
    directives = directive_loader.create_directive_instances(
        discovered_directive_classes, 
        available_primitives=primitives_dict
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
