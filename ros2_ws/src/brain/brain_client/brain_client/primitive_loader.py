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
import json
import h5py
import re
from typing import Dict, List, Type, Optional
from pathlib import Path

from brain_client.primitive_types import Primitive


class PrimitiveLoader:
    """
    Dynamically loads primitive classes from specified directories.
    """
    
    def __init__(self, logger):
        self.logger = logger
        
    def discover_primitives_in_directory(self, directory_path: str) -> Dict[str, Type[Primitive]]:
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
        primitives = {}
        module_name = file_path.stem
        
        # Load the module
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            self.logger.warning(f"Could not load spec for {file_path}")
            return primitives
            
        module = importlib.util.module_from_spec(spec)
        
        # Add the root directory to sys.path for imports to work
        maurice_prod_dir = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
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
        # Check that required abstract methods are implemented
        required_methods = ['name', 'execute', 'cancel']
        for method_name in required_methods:
            if not hasattr(primitive_class, method_name):
                self.logger.error(f"Primitive {primitive_class.__name__} missing required method: {method_name}")
                return False
                
        # Check that name is a property
        if not hasattr(primitive_class, 'name') or not isinstance(primitive_class.name, property):
            self.logger.error(f"Primitive {primitive_class.__name__} name must be a property")
            return False
            
        return True
    
    def _get_primitive_name(self, primitive_class: Type[Primitive]) -> str:
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
        # Insert underscore before uppercase letters that follow lowercase letters
        s1 = re.sub('([a-z0-9])([A-Z])', r'\1_\2', class_name)
        return s1.lower()
    
    def load_primitives_from_directories(self, directories: List[str]) -> Dict[str, Type[Primitive]]:
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

    def validate_physical_primitive(self, primitive_dir: str, metadata: dict) -> tuple:
        """Validate a physical primitive.
        
        Returns:
            tuple: (is_valid: bool, is_in_training: bool)
                - is_valid: True if the primitive can be loaded (either ready or in training)
                - is_in_training: True if the primitive is a learned type missing its checkpoint
        """
        primitive_type = metadata.get('type', '').lower()
        execution = metadata.get('execution', {})
        
        if primitive_type == 'learned':
            return self._validate_learned_primitive(primitive_dir, execution)
        elif primitive_type == 'replay':
            is_valid = self._validate_replay_primitive(primitive_dir, execution)
            return (is_valid, False)  # Replay primitives are never "in training"
        else:
            self.logger.warning(f"Unknown primitive type '{primitive_type}' in {primitive_dir}")
            return (True, False)  # Allow unknown types but log warning
    
    def _validate_learned_primitive(self, primitive_dir: str, execution: dict) -> tuple:
        """Validate a learned primitive.
        
        Returns:
            tuple: (is_valid: bool, is_in_training: bool)
        """
        checkpoint_file = execution.get('checkpoint')
        
        # If no checkpoint specified, it's in training
        if not checkpoint_file:
            self.logger.info(f"Learned primitive in {primitive_dir} has no checkpoint - marked as in_training")
            return (True, True)  # Valid but in training
        
        checkpoint_path = os.path.join(primitive_dir, checkpoint_file)
        if not os.path.exists(checkpoint_path):
            self.logger.info(f"Learned primitive checkpoint not found: {checkpoint_path} - marked as in_training")
            return (True, True)  # Valid but in training
        
        # Check for stats file (optional but commonly needed)
        stats_file = execution.get('stats_file', 'dataset_stats.pt')
        stats_path = os.path.join(primitive_dir, stats_file)
        if not os.path.exists(stats_path):
            self.logger.warning(f"Learned primitive stats file not found: {stats_path} (optional)")
        
        self.logger.info(f"Learned primitive validation passed: {primitive_dir}")
        return (True, False)  # Valid and ready
    
    def _validate_replay_primitive_internal(self, primitive_dir: str, execution: dict) -> bool:
        """Internal validation for replay primitives. Returns bool for validity."""
        replay_file = execution.get('replay_file')
        if not replay_file:
            self.logger.error(f"Replay primitive in {primitive_dir} missing replay_file in execution config")
            return False
        
        replay_path = os.path.join(primitive_dir, replay_file)
        if not os.path.exists(replay_path):
            self.logger.error(f"Replay primitive file not found: {replay_path}")
            return False
        
        # Validate H5 file structure
        try:
            with h5py.File(replay_path, 'r') as h5file:
                if 'action' not in h5file:
                    self.logger.error(f"Replay file {replay_path} missing required 'action' dataset")
                    return False
                
                actions = h5file['action'][:]
                if actions.shape[0] == 0:
                    self.logger.error(f"Replay file {replay_path} contains no actions")
                    return False
        
        except Exception as e:
            self.logger.error(f"Failed to validate replay file {replay_path}: {e}")
            return False
        
        self.logger.info(f"Replay primitive validation passed: {primitive_dir}")
        return True

    def _validate_replay_primitive(self, primitive_dir: str, execution: dict) -> bool:
        """Validate a replay primitive. Returns bool for backwards compatibility."""
        return self._validate_replay_primitive_internal(primitive_dir, execution)

