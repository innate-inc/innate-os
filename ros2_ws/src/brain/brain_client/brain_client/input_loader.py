#!/usr/bin/env python3
"""
Dynamic Input Device Loader

This module provides functionality to dynamically discover and load input device classes
from specified directories. Similar to primitive_loader.py and directive_loader.py.
"""

import os
import sys
import importlib.util
import inspect
from typing import Dict, List, Type
from pathlib import Path

from brain_client.input_types import InputDevice


class InputLoader:
    """
    Dynamically loads input device classes from specified directories.
    """
    
    def __init__(self, logger):
        self.logger = logger
        self._loaded_inputs: Dict[str, Type[InputDevice]] = {}
        
    def discover_inputs_in_directory(self, directory_path: str) -> Dict[str, Type[InputDevice]]:
        """
        Scans a directory for Python files and attempts to load input device classes.
        
        Args:
            directory_path: Path to directory containing input device files
            
        Returns:
            Dictionary mapping input device names to their classes
        """
        inputs = {}
        directory = Path(directory_path)
        
        if not directory.exists():
            self.logger.warning(f"Input device directory does not exist: {directory_path}")
            return inputs
            
        if not directory.is_dir():
            self.logger.warning(f"Path is not a directory: {directory_path}")
            return inputs
            
        self.logger.info(f"Scanning for input devices in: {directory_path}")
        
        # Look for Python files ending in _input.py
        python_files = [f for f in directory.glob("*_input.py") 
                       if f.name not in ["__init__.py", "input_types.py"] and not f.name.startswith("_")]
        
        for py_file in python_files:
            try:
                discovered = self._load_inputs_from_file(py_file)
                inputs.update(discovered)
            except Exception as e:
                self.logger.error(f"Error loading input devices from {py_file}: {e}")
                
        self.logger.info(f"Discovered {len(inputs)} input devices in {directory_path}")
        return inputs
    
    def _load_inputs_from_file(self, file_path: Path) -> Dict[str, Type[InputDevice]]:
        """
        Loads input device classes from a single Python file.
        
        Args:
            file_path: Path to the Python file
            
        Returns:
            Dictionary mapping input device names to their classes
        """
        inputs = {}
        module_name = file_path.stem
        
        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return inputs
            
        module = importlib.util.module_from_spec(spec)
        
        # Add the root directory to sys.path for imports to work
        innate_os_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        if innate_os_root not in sys.path:
            sys.path.insert(0, innate_os_root)
            
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.error(f"Error executing module {module_name}: {e}")
            return inputs
        finally:
            # Remove from sys.path if we added it
            if innate_os_root in sys.path:
                sys.path.remove(innate_os_root)
        
        # Find all classes in the module that inherit from InputDevice
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (obj != InputDevice and 
                issubclass(obj, InputDevice) and 
                obj.__module__ == module.__name__):
                
                # Validate the input device class
                if self._validate_input_class(obj):
                    input_name = self._get_input_name(obj)
                    inputs[input_name] = obj
                    self.logger.debug(f"Loaded input device: {input_name} from {file_path}")
                else:
                    self.logger.warning(f"Invalid input device class: {name} in {file_path}")
                    
        return inputs
    
    def _validate_input_class(self, input_class: Type[InputDevice]) -> bool:
        """
        Validates that an input device class is properly implemented.
        
        Args:
            input_class: The input device class to validate
            
        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ['name', 'on_open', 'on_close']
            for method_name in required_methods:
                if not hasattr(input_class, method_name):
                    self.logger.error(f"Input device {input_class.__name__} missing required method: {method_name}")
                    return False
                    
            # Check that name is a property
            if not isinstance(getattr(type(input_class), 'name', None), property):
                self.logger.error(f"Input device {input_class.__name__} 'name' must be a property")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating input device {input_class.__name__}: {e}")
            return False
    
    def _get_input_name(self, input_class: Type[InputDevice]) -> str:
        """
        Gets the input device name by creating a temporary instance.
        
        Args:
            input_class: The input device class
            
        Returns:
            The input device's name
        """
        try:
            temp_instance = input_class(logger=None)
            return temp_instance.name
        except Exception as e:
            self.logger.debug(f"Could not get name from input device {input_class.__name__}: {e}")
            # Fallback to class name converted to snake_case
            fallback_name = self._class_name_to_snake_case(input_class.__name__)
            self.logger.debug(f"Using fallback name: {fallback_name}")
            return fallback_name
    
    def _class_name_to_snake_case(self, class_name: str) -> str:
        """Convert CamelCase to snake_case."""
        import re
        # Remove "Input" suffix if present
        if class_name.endswith("Input"):
            class_name = class_name[:-5]  # Remove "Input"
        
        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', class_name)
        return s1.lower()
    
    def load_inputs_from_directories(self, directories: List[str]) -> Dict[str, Type[InputDevice]]:
        """
        Load input devices from multiple directories.
        
        Args:
            directories: List of directory paths to scan
            
        Returns:
            Dictionary mapping input device names to their classes
        """
        all_inputs = {}
        
        for directory in directories:
            try:
                discovered = self.discover_inputs_in_directory(directory)
                
                # Check for name conflicts
                for name, input_class in discovered.items():
                    if name in all_inputs:
                        self.logger.warning(
                            f"Input device name conflict: '{name}' found in both "
                            f"{all_inputs[name].__module__} and {input_class.__module__}. "
                            f"Using the latter."
                        )
                    all_inputs[name] = input_class
                    
            except Exception as e:
                self.logger.error(f"Error loading input devices from {directory}: {e}")
                
        return all_inputs
    
    def create_input_instances(self, input_classes: Dict[str, Type[InputDevice]], 
                               logger) -> Dict[str, InputDevice]:
        """
        Create instances of input device classes.
        
        Args:
            input_classes: Dictionary of input device name to class mappings
            logger: Logger instance to pass to input devices
            
        Returns:
            Dictionary mapping input device names to their instances
        """
        input_instances = {}
        
        for input_name, input_class in input_classes.items():
            try:
                input_instance = input_class(logger)
                input_instances[input_name] = input_instance
                self.logger.debug(f"Created input device instance: {input_name}")
            except Exception as e:
                self.logger.error(f"Error creating input device instance {input_name}: {e}")
                
        return input_instances


