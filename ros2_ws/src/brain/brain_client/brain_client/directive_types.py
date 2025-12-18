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
    def id(self) -> str:
        """
        The name of the directive (used as identifier).
        Must be defined by every subclass.
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """
        The human-readable display name of the directive.
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

    @property
    def display_icon(self) -> Optional[str]:
        """
        Optional path to a 32x32 pixel icon asset for this directive.
        
        Subclasses can override this property to specify an icon.
        Default: return None (no icon).
        
        Example:
            return "assets/my_directive_icon.png"
        """
        return None

    def get_inputs(self) -> List[str]:
        """
        Returns a list of input device names that should be active
        when this directive is running.

        Subclasses can override this method to specify required inputs.
        Default: return empty list (no input devices required).

        Example:
            return ["micro", "camera"]
        """
        return []

