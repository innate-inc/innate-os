#!/usr/bin/env python3
"""
Dynamic Directive Loader

This module provides functionality to dynamically discover and load directive classes
from specified directories. It validates that directives inherit from the Directive base class
and can automatically register them with the brain client.
"""

import os
import sys
import importlib.util
import inspect
from typing import Dict, List, Type, Optional
from pathlib import Path

from brain_client.directive_types import Directive


class DirectiveLoader:
    """
    Dynamically loads directive classes from specified directories.
    """
    
    def __init__(self, logger):
        self.logger = logger
        self._loaded_directives: Dict[str, Type[Directive]] = {}
        
    def discover_directives_in_directory(self, directory_path: str) -> Dict[str, Type[Directive]]:
        """
        Scans a directory for Python files and attempts to load directive classes.
        
        Args:
            directory_path: Path to directory containing directive files
            
        Returns:
            Dictionary mapping directive names to their classes
        """
        directives = {}
        directory = Path(directory_path)
        
        if not directory.exists():
            self.logger.warning(f"Directive directory does not exist: {directory_path}")
            return directives
            
        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return directives
            
        self.logger.info(f"Scanning for directives in: {directory_path}")
        
        # Look for Python files (excluding __init__.py, __pycache__, and types.py)
        python_files = [f for f in directory.glob("*.py") 
                       if f.name not in ["__init__.py", "types.py"] and not f.name.startswith("_")]
        
        for py_file in python_files:
            try:
                discovered = self._load_directives_from_file(py_file)
                directives.update(discovered)
            except Exception as e:
                self.logger.error(f"Error loading directives from {py_file}: {e}")
                
        self.logger.info(f"Discovered {len(directives)} directives in {directory_path}")
        return directives
    
    def _load_directives_from_file(self, file_path: Path) -> Dict[str, Type[Directive]]:
        """
        Loads directive classes from a single Python file.
        
        Args:
            file_path: Path to the Python file
            
        Returns:
            Dictionary mapping directive names to their classes
        """
        directives = {}
        module_name = file_path.stem
        
        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return directives
            
        module = importlib.util.module_from_spec(spec)
        
        # Add the parent directory to sys.path temporarily for imports to work
        parent_dir = str(file_path.parent.parent.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            return directives
        finally:
            # Remove from sys.path if we added it
            if parent_dir in sys.path:
                sys.path.remove(parent_dir)
        
        # Find all classes in the module that inherit from Directive
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (obj != Directive and 
                issubclass(obj, Directive) and 
                obj.__module__ == module.__name__):
                
                # Validate the directive class
                if self._validate_directive_class(obj):
                    directive_name = self._get_directive_name(obj)
                    directives[directive_name] = obj
                    self.logger.debug(f"Loaded directive: {directive_name} from {file_path}")
                else:
                    self.logger.warning(f"Invalid directive class: {name} in {file_path}")
                    
        return directives
    
    def _validate_directive_class(self, directive_class: Type[Directive]) -> bool:
        """
        Validates that a directive class is properly implemented.
        
        Args:
            directive_class: The directive class to validate
            
        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ['name', 'get_primitives', 'get_prompt']
            for method_name in required_methods:
                if not hasattr(directive_class, method_name):
                    self.logger.error(f"Directive {directive_class.__name__} missing required method: {method_name}")
                    return False
                    
            # Check that name is a property
            if not hasattr(directive_class, 'name') or not isinstance(directive_class.name, property):
                self.logger.error(f"Directive {directive_class.__name__} name must be a property")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating directive {directive_class.__name__}: {e}")
            return False
    
    def _get_directive_name(self, directive_class: Type[Directive]) -> str:
        """
        Gets the directive name by creating a temporary instance.
        This is needed because the name is a property that requires instantiation.
        
        Args:
            directive_class: The directive class
            
        Returns:
            The directive's name
        """
        try:
            temp_instance = directive_class()
            return temp_instance.name
        except Exception as e:
            self.logger.debug(f"Could not get name from directive {directive_class.__name__}: {e}")
            # Fallback to class name converted to snake_case
            fallback_name = self._class_name_to_snake_case(directive_class.__name__)
            self.logger.debug(f"Using fallback name: {fallback_name}")
            return fallback_name
    
    def _class_name_to_snake_case(self, class_name: str) -> str:
        """Convert CamelCase to snake_case."""
        import re
        # Remove "Directive" suffix if present
        if class_name.endswith("Directive"):
            class_name = class_name[:-9]  # Remove "Directive"
        
        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', class_name)
        return s1.lower()
    
    def load_directives_from_directories(self, directories: List[str]) -> Dict[str, Type[Directive]]:
        """
        Load directives from multiple directories.
        
        Args:
            directories: List of directory paths to scan
            
        Returns:
            Dictionary mapping directive names to their classes
        """
        all_directives = {}
        
        for directory in directories:
            try:
                discovered = self.discover_directives_in_directory(directory)
                
                # Check for name conflicts
                for name, directive_class in discovered.items():
                    if name in all_directives:
                        self.logger.warning(
                            f"Directive name conflict: '{name}' found in both "
                            f"{all_directives[name].__module__} and {directive_class.__module__}. "
                            f"Using the latter."
                        )
                    all_directives[name] = directive_class
                    
            except Exception as e:
                self.logger.error(f"Error loading directives from {directory}: {e}")
                
        return all_directives
    
    def create_directive_instances(self, directive_classes: Dict[str, Type[Directive]], 
                                   available_primitives: Optional[Dict[str, any]] = None) -> Dict[str, Directive]:
        """
        Create instances of directive classes.
        
        Args:
            directive_classes: Dictionary of directive name to class mappings
            available_primitives: Optional dictionary of available primitive names to validate against
            
        Returns:
            Dictionary mapping directive names to their instances
        """
        directive_instances = {}
        
        for directive_name, directive_class in directive_classes.items():
            try:
                directive_instance = directive_class()
                
                # Validate primitives if available_primitives dict is provided
                if available_primitives is not None:
                    self._validate_directive_primitives(directive_instance, available_primitives)
                
                directive_instances[directive_name] = directive_instance
                self.logger.debug(f"Created directive instance: {directive_name}")
            except Exception as e:
                self.logger.error(f"Error creating directive instance {directive_name}: {e}")
                
        return directive_instances
    
    def _validate_directive_primitives(self, directive_instance: Directive, 
                                       available_primitives: Dict[str, any]) -> None:
        """
        Validates that all primitives referenced by a directive have corresponding
        primitive files available.
        
        Args:
            directive_instance: The directive instance to validate
            available_primitives: Dictionary of available primitive names
            
        Raises:
            Warning if a primitive is not found (logged, not raised)
        """
        try:
            directive_primitives = directive_instance.get_primitives()
            missing_primitives = []
            
            for primitive_name in directive_primitives:
                if primitive_name not in available_primitives:
                    missing_primitives.append(primitive_name)
            
            if missing_primitives:
                self.logger.warning(
                    f"Directive '{directive_instance.name}' references primitives that are not available: "
                    f"{missing_primitives}. Available primitives: {list(available_primitives.keys())}"
                )
            else:
                self.logger.debug(
                    f"Directive '{directive_instance.name}' primitives validated successfully: "
                    f"{directive_primitives}"
                )
        except Exception as e:
            self.logger.error(
                f"Error validating primitives for directive '{directive_instance.name}': {e}"
            )
