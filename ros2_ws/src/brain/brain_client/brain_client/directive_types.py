#!/usr/bin/env python3
"""
Directive Type Definitions

Base class and types for robot directives.
"""
from abc import ABC, abstractmethod
from typing import List, Optional


class Directive(ABC):
    """
    Base class for all directives.

    A directive provides personality and behavior guidelines for the robot,
    along with the list of primitives that should be available when this
    directive is active.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        The name of the directive.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def get_primitives(self) -> List[str]:
        """
        Returns a list of primitive names that should be available
        when this directive is active.

        Subclasses must implement this method.
        """
        pass

    @abstractmethod
    def get_prompt(self) -> Optional[str]:
        """
        Returns the prompt/description for this directive.
        This defines the robot's personality and behavior guidelines.

        Subclasses must implement this method.
        """
        pass

