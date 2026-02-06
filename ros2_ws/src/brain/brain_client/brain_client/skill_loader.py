#!/usr/bin/env python3
"""
Dynamic Skill Loader

This module provides functionality to dynamically discover and load skill classes
from specified directories. It validates that skills inherit from the Skill base class
and can automatically register them with the execution server.
"""

import importlib.util
import inspect
import json
import os
import re
import sys
from pathlib import Path

import h5py

from brain_client.skill_types import Skill


class SkillLoader:
    """
    Dynamically loads skill classes from specified directories.
    """

    def __init__(self, logger):
        self.logger = logger

    def discover_skills_in_directory(self, directory_path: str) -> dict[str, type[Skill]]:
        skills = {}
        directory = Path(directory_path)

        if not directory.exists():
            self.logger.warning(f"Skill directory does not exist: {directory_path}")
            return skills

        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return skills

        self.logger.info(f"Scanning for skills in: {directory_path}")

        # Look for Python files (excluding __init__.py and __pycache__)
        python_files = [f for f in directory.glob("*.py") if f.name != "__init__.py" and not f.name.startswith("_")]

        for py_file in python_files:
            try:
                discovered = self._load_skills_from_file(py_file)
                skills.update(discovered)
            except Exception as e:
                self.logger.error(f"Error loading skills from {py_file}: {e}")

        self.logger.info(f"Discovered {len(skills)} skills in {directory_path}")
        return skills

    def _load_skills_from_file(self, file_path: Path) -> dict[str, type[Skill]]:
        skills = {}
        module_name = file_path.stem

        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return skills

        module = importlib.util.module_from_spec(spec)

        # Add the root directory to sys.path for imports to work
        maurice_prod_dir = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
        if maurice_prod_dir not in sys.path:
            sys.path.insert(0, maurice_prod_dir)

        try:
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            return skills
        finally:
            # Remove from sys.path if we added it
            if maurice_prod_dir in sys.path:
                sys.path.remove(maurice_prod_dir)

        # Find all classes in the module that inherit from Skill
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj != Skill and issubclass(obj, Skill) and obj.__module__ == module.__name__:
                # Validate the skill class
                if self._validate_skill_class(obj):
                    skill_name = self._get_skill_name(obj)
                    skills[skill_name] = obj
                    self.logger.debug(f"Loaded skill: {skill_name} from {file_path}")
                else:
                    self.logger.warning(f"Invalid skill class: {name} in {file_path}")

        return skills

    def _validate_skill_class(self, skill_class: type[Skill]) -> bool:
        # Check that required abstract methods are implemented
        required_methods = ["name", "execute", "cancel"]
        for method_name in required_methods:
            if not hasattr(skill_class, method_name):
                self.logger.error(f"Skill {skill_class.__name__} missing required method: {method_name}")
                return False

        # Check that name is a property
        if not hasattr(skill_class, "name") or not isinstance(skill_class.name, property):
            self.logger.error(f"Skill {skill_class.__name__} name must be a property")
            return False

        return True

    def _get_skill_name(self, skill_class: type[Skill]) -> str:
        try:
            # Create a temporary logger for this purpose
            import logging

            temp_logger = logging.getLogger(f"temp_{skill_class.__name__}")
            temp_instance = skill_class(temp_logger)
            return temp_instance.name
        except Exception as e:
            self.logger.debug(f"Could not get name from skill {skill_class.__name__}: {e}")
            # Fallback to class name converted to snake_case
            fallback_name = self._class_name_to_snake_case(skill_class.__name__)
            self.logger.debug(f"Using fallback name: {fallback_name}")
            return fallback_name

    def _class_name_to_snake_case(self, class_name: str) -> str:
        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub("([a-z0-9])([A-Z])", r"\1_\2", class_name)
        return s1.lower()

    def reload_skill_by_name(self, skill_name: str, directories: list[str]) -> type[Skill] | None:
        """
        Reload a specific skill by name from the given directories.
        
        Args:
            skill_name: The name of the skill to reload
            directories: List of directories to search for the skill
            
        Returns:
            The reloaded skill class, or None if not found
        """
        for directory in directories:
            directory_path = Path(directory)
            if not directory_path.exists():
                continue
                
            # Search for python files that might contain this skill
            python_files = [f for f in directory_path.glob("*.py") 
                          if f.name != "__init__.py" and not f.name.startswith("_")]
            
            for py_file in python_files:
                try:
                    # Try to load skills from this file
                    discovered = self._load_skills_from_file(py_file)
                    if skill_name in discovered:
                        self.logger.info(f"Reloaded skill '{skill_name}' from {py_file}")
                        return discovered[skill_name]
                except Exception as e:
                    self.logger.debug(f"Error checking {py_file} for skill {skill_name}: {e}")
        
        self.logger.warning(f"Could not find skill '{skill_name}' in any directory")
        return None

    def reload_skills_by_names(self, skill_names: list[str], directories: list[str]) -> dict[str, type[Skill]]:
        """
        Reload specific skills by name.
        
        Args:
            skill_names: List of skill names to reload
            directories: List of directories to search
            
        Returns:
            Dictionary of reloaded skill classes
        """
        reloaded = {}
        for name in skill_names:
            skill_class = self.reload_skill_by_name(name, directories)
            if skill_class is not None:
                reloaded[name] = skill_class
        return reloaded

    def load_skills_from_directories(self, directories: list[str]) -> dict[str, type[Skill]]:
        all_skills = {}

        for directory in directories:
            try:
                discovered = self.discover_skills_in_directory(directory)

                # Check for name conflicts
                for name, skill_class in discovered.items():
                    if name in all_skills:
                        self.logger.warning(
                            f"Skill name conflict: '{name}' found in both "
                            f"{all_skills[name].__module__} and {skill_class.__module__}. "
                            f"Using the latter."
                        )
                    all_skills[name] = skill_class

            except Exception as e:
                self.logger.error(f"Error loading skills from {directory}: {e}")

        return all_skills

    def validate_physical_skill(self, skill_dir: str, metadata: dict) -> tuple:
        """Validate a physical skill.

        Returns:
            tuple: (is_valid: bool, is_in_training: bool, episode_count: int)
                - is_valid: True if the skill can be loaded (either ready or in training)
                - is_in_training: True if the skill is a learned type missing its checkpoint
                - episode_count: Number of recorded episodes (0 if not applicable or not found)
        """
        skill_type = metadata.get("type", "").lower()
        execution = metadata.get("execution", {})

        if skill_type == "learned":
            is_valid, is_in_training = self._validate_learned_skill(skill_dir, execution)
            episode_count = self._get_episode_count(skill_dir)
            return (is_valid, is_in_training, episode_count)
        elif skill_type == "replay":
            is_valid = self._validate_replay_skill(skill_dir, execution)
            return (
                is_valid,
                False,
                0,
            )  # Replay skills are never "in training", no episodes
        else:
            self.logger.warning(f"Unknown skill type '{skill_type}' in {skill_dir}")
            return (True, False, 0)  # Allow unknown types but log warning

    def _validate_learned_skill(self, skill_dir: str, execution: dict) -> tuple:
        """Validate a learned skill.

        Returns:
            tuple: (is_valid: bool, is_in_training: bool)
        """
        checkpoint_file = execution.get("checkpoint")
        # If no checkpoint specified, it's in training
        if not checkpoint_file:
            self.logger.info(f"Learned skill in {skill_dir} has no checkpoint - marked as in_training")
            return (True, True)  # Valid but in training

        checkpoint_path = os.path.join(skill_dir, checkpoint_file)
        if not os.path.exists(checkpoint_path):
            self.logger.info(f"Learned skill checkpoint not found: {checkpoint_path} - marked as in_training")
            return (True, True)  # Valid but in training

        # Check for stats file (optional but commonly needed)
        stats_file = execution.get("stats_file", "dataset_stats.pt")
        stats_path = os.path.join(skill_dir, stats_file)
        if not os.path.exists(stats_path):
            self.logger.warning(f"Learned skill stats file not found: {stats_path} (optional)")

        self.logger.info(f"Learned skill validation passed: {skill_dir}")
        return (True, False)  # Valid and ready

    def _get_episode_count(self, skill_dir: str) -> int:
        """Get the number of recorded episodes for a learned skill.

        Looks for data/dataset_metadata.json and reads the number_of_episodes field.

        Args:
            skill_dir: Path to the skill directory.

        Returns:
            int: Number of episodes, or 0 if not found.
        """
        dataset_metadata_path = os.path.join(skill_dir, "data", "dataset_metadata.json")

        if not os.path.exists(dataset_metadata_path):
            return 0

        try:
            with open(dataset_metadata_path) as f:
                dataset_metadata = json.load(f)
                return dataset_metadata.get("number_of_episodes", 0)
        except Exception as e:
            self.logger.warning(f"Error reading dataset metadata from {dataset_metadata_path}: {e}")
            return 0

    def _validate_replay_skill_internal(self, skill_dir: str, execution: dict) -> bool:
        """Internal validation for replay skills. Returns bool for validity."""
        replay_file = execution.get("replay_file")
        if not replay_file:
            self.logger.error(f"Replay skill in {skill_dir} missing replay_file in execution config")
            return False

        replay_path = os.path.join(skill_dir, replay_file)
        if not os.path.exists(replay_path):
            self.logger.error(f"Replay skill file not found: {replay_path}")
            return False

        # Validate H5 file structure
        try:
            with h5py.File(replay_path, "r") as h5file:
                if "action" not in h5file:
                    self.logger.error(f"Replay file {replay_path} missing required 'action' dataset")
                    return False

                actions = h5file["action"][:]
                if actions.shape[0] == 0:
                    self.logger.error(f"Replay file {replay_path} contains no actions")
                    return False

        except Exception as e:
            self.logger.error(f"Failed to validate replay file {replay_path}: {e}")
            return False

        self.logger.info(f"Replay skill validation passed: {skill_dir}")
        return True

    def _validate_replay_skill(self, skill_dir: str, execution: dict) -> bool:
        """Validate a replay skill. Returns bool for backwards compatibility."""
        return self._validate_replay_skill_internal(skill_dir, execution)
