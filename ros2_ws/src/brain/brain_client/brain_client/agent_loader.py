#!/usr/bin/env python3
"""
Dynamic Agent Loader

This module provides functionality to dynamically discover and load agent classes
from specified directories. It validates that agents inherit from the Agent base class
and can automatically register them with the brain client.
"""

import base64
import os
import sys
import importlib.util
import inspect
from typing import Dict, List, Type, Optional
from pathlib import Path

from brain_client.agent_types import Agent


class AgentLoader:
    """
    Dynamically loads agent classes from specified directories.
    """

    def __init__(self, logger):
        self.logger = logger
        self._loaded_agents: Dict[str, Type[Agent]] = {}

    def discover_agents_in_directory(
        self, directory_path: str
    ) -> Dict[str, Type[Agent]]:
        """
        Scans a directory for Python files and attempts to load agent classes.

        Args:
            directory_path: Path to directory containing agent files

        Returns:
            Dictionary mapping agent names to their classes
        """
        agents = {}
        directory = Path(directory_path)

        if not directory.exists():
            self.logger.warning(f"Agent directory does not exist: {directory_path}")
            return agents

        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return agents

        self.logger.info(f"Scanning for agents in: {directory_path}")

        # Look for Python files (excluding __init__.py, __pycache__, and types.py)
        python_files = [
            f
            for f in directory.glob("*.py")
            if f.name not in ["__init__.py", "types.py"] and not f.name.startswith("_")
        ]

        for py_file in python_files:
            try:
                discovered = self._load_agents_from_file(py_file)
                agents.update(discovered)
            except Exception as e:
                self.logger.error(f"Error loading agents from {py_file}: {e}")

        self.logger.info(f"Discovered {len(agents)} agents in {directory_path}")
        return agents

    def _load_agents_from_file(self, file_path: Path) -> Dict[str, Type[Agent]]:
        """
        Loads agent classes from a single Python file.

        Args:
            file_path: Path to the Python file

        Returns:
            Dictionary mapping agent names to their classes
        """
        agents = {}
        module_name = file_path.stem

        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return agents

        module = importlib.util.module_from_spec(spec)

        # Add the root directory to sys.path for imports to work
        maurice_prod_dir = os.environ.get(
            "INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os")
        )
        if maurice_prod_dir not in sys.path:
            sys.path.insert(0, maurice_prod_dir)

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            return agents
        finally:
            # Remove from sys.path if we added it
            if maurice_prod_dir in sys.path:
                sys.path.remove(maurice_prod_dir)

        # Find all classes in the module that inherit from Agent
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                obj != Agent
                and issubclass(obj, Agent)
                and obj.__module__ == module.__name__
            ):

                # Validate the agent class
                if self._validate_agent_class(obj):
                    agent_name = self._get_agent_name(obj)
                    agents[agent_name] = obj
                    self.logger.debug(f"Loaded agent: {agent_name} from {file_path}")
                else:
                    self.logger.warning(f"Invalid agent class: {name} in {file_path}")

        return agents

    def _validate_agent_class(self, agent_class: Type[Agent]) -> bool:
        """
        Validates that an agent class is properly implemented.

        Args:
            agent_class: The agent class to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ["id", "display_name", "get_skills", "get_prompt"]
            for method_name in required_methods:
                if not hasattr(agent_class, method_name):
                    self.logger.error(
                        f"Agent {agent_class.__name__} missing required method: {method_name}"
                    )
                    return False

            # Check that id is a property
            if not hasattr(agent_class, "id") or not isinstance(
                agent_class.id, property
            ):
                self.logger.error(f"Agent {agent_class.__name__} id must be a property")
                return False

            # Check that display_name is a property
            if not hasattr(agent_class, "display_name") or not isinstance(
                agent_class.display_name, property
            ):
                self.logger.error(
                    f"Agent {agent_class.__name__} display_name must be a property"
                )
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating agent {agent_class.__name__}: {e}")
            return False

    def _get_agent_name(self, agent_class: Type[Agent]) -> str:
        """
        Gets the agent name by creating a temporary instance.
        This is needed because the name is a property that requires instantiation.

        Args:
            agent_class: The agent class

        Returns:
            The agent's name
        """
        try:
            temp_instance = agent_class()
            return temp_instance.id
        except Exception as e:
            self.logger.debug(
                f"Could not get name from agent {agent_class.__name__}: {e}"
            )
            # Fallback to class name converted to snake_case
            fallback_name = self._class_name_to_snake_case(agent_class.__name__)
            self.logger.debug(f"Using fallback name: {fallback_name}")
            return fallback_name

    def _class_name_to_snake_case(self, class_name: str) -> str:
        """Convert CamelCase to snake_case."""
        import re

        # Remove "Agent" suffix if present
        if class_name.endswith("Agent"):
            class_name = class_name[:-5]  # Remove "Agent"
        # Also handle legacy "Directive" suffix
        elif class_name.endswith("Directive"):
            class_name = class_name[:-9]  # Remove "Directive"

        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub("([a-z0-9])([A-Z])", r"\1_\2", class_name)
        return s1.lower()

    def reload_agent_by_name(self, agent_name: str, directories: List[str]) -> Optional[Type[Agent]]:
        """
        Reload a specific agent by name from the given directories.
        
        Args:
            agent_name: The name of the agent to reload
            directories: List of directories to search for the agent
            
        Returns:
            The reloaded agent class, or None if not found
        """
        for directory in directories:
            directory_path = Path(directory)
            if not directory_path.exists():
                continue
                
            # Search for python files that might contain this agent
            python_files = [
                f for f in directory_path.glob("*.py")
                if f.name not in ["__init__.py", "types.py"] and not f.name.startswith("_")
            ]
            
            for py_file in python_files:
                try:
                    discovered = self._load_agents_from_file(py_file)
                    if agent_name in discovered:
                        self.logger.info(f"Reloaded agent '{agent_name}' from {py_file}")
                        return discovered[agent_name]
                except Exception as e:
                    self.logger.debug(f"Error checking {py_file} for agent {agent_name}: {e}")
        
        self.logger.warning(f"Could not find agent '{agent_name}' in any directory")
        return None

    def reload_agents_by_names(self, agent_names: List[str], directories: List[str]) -> Dict[str, Type[Agent]]:
        """
        Reload specific agents by name.
        
        Args:
            agent_names: List of agent names to reload
            directories: List of directories to search
            
        Returns:
            Dictionary of reloaded agent classes
        """
        reloaded = {}
        for name in agent_names:
            agent_class = self.reload_agent_by_name(name, directories)
            if agent_class is not None:
                reloaded[name] = agent_class
        return reloaded

    def load_agents_from_directories(
        self, directories: List[str]
    ) -> Dict[str, Type[Agent]]:
        """
        Load agents from multiple directories.

        Args:
            directories: List of directory paths to scan

        Returns:
            Dictionary mapping agent names to their classes
        """
        all_agents = {}

        for directory in directories:
            try:
                discovered = self.discover_agents_in_directory(directory)

                # Check for name conflicts
                for name, agent_class in discovered.items():
                    if name in all_agents:
                        self.logger.warning(
                            f"Agent name conflict: '{name}' found in both "
                            f"{all_agents[name].__module__} and {agent_class.__module__}. "
                            f"Using the latter."
                        )
                    all_agents[name] = agent_class

            except Exception as e:
                self.logger.error(f"Error loading agents from {directory}: {e}")

        return all_agents

    def create_agent_instances(
        self,
        agent_classes: Dict[str, Type[Agent]],
        available_skills: Optional[Dict[str, any]] = None,
        agents_directory: Optional[str] = None,
    ) -> Dict[str, Agent]:
        """
        Create instances of agent classes.

        Args:
            agent_classes: Dictionary of agent name to class mappings
            available_skills: Optional dictionary of available skill names to validate against
            agents_directory: Optional path to agents directory for loading icons

        Returns:
            Dictionary mapping agent names to their instances
        """
        agent_instances = {}

        for agent_name, agent_class in agent_classes.items():
            try:
                agent_instance = agent_class()

                # Load and encode the display icon as base64
                self._load_display_icon(agent_instance, agents_directory)

                # Validate skills if available_skills dict is provided
                if available_skills is not None:
                    self._validate_agent_skills(agent_instance, available_skills)

                agent_instances[agent_name] = agent_instance
                self.logger.debug(f"Created agent instance: {agent_name}")
            except Exception as e:
                self.logger.error(f"Error creating agent instance {agent_name}: {e}")

        return agent_instances

    def _load_display_icon(
        self, agent_instance: Agent, agents_directory: Optional[str]
    ) -> None:
        """
        Load and encode the agent's display icon as base64.

        Args:
            agent_instance: The agent instance
            agents_directory: Path to the agents directory
        """
        # Initialize the attribute for storing base64 icon data
        agent_instance.display_icon_data = None

        if not agent_instance.display_icon or not agents_directory:
            return

        icon_path = os.path.join(agents_directory, agent_instance.display_icon)
        if os.path.exists(icon_path):
            try:
                with open(icon_path, "rb") as f:
                    icon_bytes = f.read()
                    agent_instance.display_icon_data = base64.b64encode(
                        icon_bytes
                    ).decode("utf-8")
                    self.logger.debug(f"Loaded icon for agent '{agent_instance.id}'")
            except Exception as e:
                self.logger.warning(
                    f"Failed to load icon for agent '{agent_instance.id}': {e}"
                )

    def _validate_agent_skills(
        self, agent_instance: Agent, available_skills: Dict[str, any]
    ) -> None:
        """
        Validates that all skills referenced by an agent have corresponding
        skill files available.

        Args:
            agent_instance: The agent instance to validate
            available_skills: Dictionary of available skill names

        Raises:
            Warning if a skill is not found (logged, not raised)
        """
        try:
            agent_skills = agent_instance.get_skills()
            missing_skills = []

            for skill_name in agent_skills:
                if skill_name not in available_skills:
                    missing_skills.append(skill_name)

            if missing_skills:
                self.logger.warning(
                    f"Agent '{agent_instance.id}' references skills that are not available: "
                    f"{missing_skills}. Available skills: {list(available_skills.keys())}"
                )
            else:
                self.logger.debug(
                    f"Agent '{agent_instance.id}' skills validated successfully: "
                    f"{agent_skills}"
                )
        except Exception as e:
            self.logger.error(
                f"Error validating skills for agent '{agent_instance.id}': {e}"
            )
