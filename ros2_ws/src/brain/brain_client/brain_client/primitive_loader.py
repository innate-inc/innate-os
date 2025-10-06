#!/usr/bin/env python3
"""
Dynamic Primitive Loader

This module provides functionality to dynamically discover and load primitive classes
from specified directories. It validates that primitives inherit from the Primitive base class
and can automatically register them with the execution server.
"""

import os
import sys
import importlib.util
import inspect
from typing import Dict, List, Type, Optional
from pathlib import Path

from brain_client.primitive_types import Primitive


class PrimitiveLoader:
    """
    Dynamically loads primitive classes from specified directories.
    """
    
    def __init__(self, logger):
        self.logger = logger
        self._loaded_primitives: Dict[str, Type[Primitive]] = {}
        
    def discover_primitives_in_directory(self, directory_path: str) -> Dict[str, Type[Primitive]]:
        """
        Scans a directory for Python files and attempts to load primitive classes.
        
        Args:
            directory_path: Path to directory containing primitive files
            
        Returns:
            Dictionary mapping primitive names to their classes
        """
        primitives = {}
        directory = Path(directory_path)
        
        if not directory.exists():
            self.logger.warning(f"Primitive directory does not exist: {directory_path}")
            return primitives
            
        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return primitives
            
        self.logger.info(f"Scanning for primitives in: {directory_path}")
        
        # Look for Python files (excluding __init__.py and __pycache__)
        python_files = [f for f in directory.glob("*.py") 
                       if f.name != "__init__.py" and not f.name.startswith("_")]
        
        for py_file in python_files:
            try:
                discovered = self._load_primitives_from_file(py_file)
                primitives.update(discovered)
            except Exception as e:
                self.logger.error(f"Error loading primitives from {py_file}: {e}")
                
        self.logger.info(f"Discovered {len(primitives)} primitives in {directory_path}")
        return primitives
    
    def _load_primitives_from_file(self, file_path: Path) -> Dict[str, Type[Primitive]]:
        """
        Loads primitive classes from a single Python file.
        
        Args:
            file_path: Path to the Python file
            
        Returns:
            Dictionary mapping primitive names to their classes
        """
        primitives = {}
        module_name = file_path.stem
        
        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return primitives
            
        module = importlib.util.module_from_spec(spec)
        
        # Add the maurice-prod root directory to sys.path for imports to work
        maurice_prod_dir = os.path.expanduser("~/maurice-prod")
        if maurice_prod_dir not in sys.path:
            sys.path.insert(0, maurice_prod_dir)
            
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            return primitives
        finally:
            # Remove from sys.path if we added it
            if maurice_prod_dir in sys.path:
                sys.path.remove(maurice_prod_dir)
        
        # Find all classes in the module that inherit from Primitive
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (obj != Primitive and 
                issubclass(obj, Primitive) and 
                obj.__module__ == module.__name__):
                
                # Validate the primitive class
                if self._validate_primitive_class(obj):
                    primitive_name = self._get_primitive_name(obj)
                    primitives[primitive_name] = obj
                    self.logger.debug(f"Loaded primitive: {primitive_name} from {file_path}")
                else:
                    self.logger.warning(f"Invalid primitive class: {name} in {file_path}")
                    
        return primitives
    
    def _validate_primitive_class(self, primitive_class: Type[Primitive]) -> bool:
        """
        Validates that a primitive class is properly implemented.
        
        Args:
            primitive_class: The primitive class to validate
            
        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ['name', 'execute', 'cancel']
            for method_name in required_methods:
                if not hasattr(primitive_class, method_name):
                    self.logger.error(f"Primitive {primitive_class.__name__} missing required method: {method_name}")
                    return False
                    
            # Try to get the name property (this will fail if not implemented properly)
            # We can't instantiate without a logger, so we'll check if the property exists
            if not hasattr(primitive_class, 'name') or not isinstance(primitive_class.name, property):
                self.logger.error(f"Primitive {primitive_class.__name__} name must be a property")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating primitive {primitive_class.__name__}: {e}")
            return False
    
    def _get_primitive_name(self, primitive_class: Type[Primitive]) -> str:
        """
        Gets the primitive name by creating a temporary instance.
        This is needed because the name is a property that requires instantiation.
        
        Args:
            primitive_class: The primitive class
            
        Returns:
            The primitive's name
        """
        try:
            # Create a temporary logger for this purpose
            import logging
            temp_logger = logging.getLogger(f"temp_{primitive_class.__name__}")
            temp_instance = primitive_class(temp_logger)
            return temp_instance.name
        except Exception as e:
            self.logger.debug(f"Could not get name from primitive {primitive_class.__name__}: {e}")
            # Fallback to class name converted to snake_case
            fallback_name = self._class_name_to_snake_case(primitive_class.__name__)
            self.logger.debug(f"Using fallback name: {fallback_name}")
            return fallback_name
    
    def _class_name_to_snake_case(self, class_name: str) -> str:
        """Convert CamelCase to snake_case."""
        import re
        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', class_name)
        return s1.lower()
    
    def load_primitives_from_directories(self, directories: List[str]) -> Dict[str, Type[Primitive]]:
        """
        Load primitives from multiple directories.
        
        Args:
            directories: List of directory paths to scan
            
        Returns:
            Dictionary mapping primitive names to their classes
        """
        all_primitives = {}
        
        for directory in directories:
            try:
                discovered = self.discover_primitives_in_directory(directory)
                
                # Check for name conflicts
                for name, primitive_class in discovered.items():
                    if name in all_primitives:
                        self.logger.warning(
                            f"Primitive name conflict: '{name}' found in both "
                            f"{all_primitives[name].__module__} and {primitive_class.__module__}. "
                            f"Using the latter."
                        )
                    all_primitives[name] = primitive_class
                    
            except Exception as e:
                self.logger.error(f"Error loading primitives from {directory}: {e}")
                
        return all_primitives

