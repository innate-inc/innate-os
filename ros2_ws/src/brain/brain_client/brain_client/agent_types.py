#!/usr/bin/env python3
"""
Agent Type Definitions

Base class and types for robot agents.
"""
from abc import ABC, abstractmethod
from typing import List, Optional


class Agent(ABC):
    """
    Base class for all agents.

    An agent provides personality and behavior guidelines for the robot,
    along with the list of skills that should be available when this
    agent is active.
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
    def get_skills(self) -> List[str]:
        """
        Returns a list of skill names that should be available
        when this agent is active.

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

    def uses_gaze(self) -> bool:
        """
        Whether this agent uses person-tracking gaze.
        When True, the robot will look at detected people during conversation
        and pause gazing during skill execution.

        Subclasses can override to enable gazing.
        Default: False.
        """
        return False
