#!/usr/bin/env python3
"""
Agent Type Definitions

Base class and types for robot agents.

Agent Types:
    - CloudAgent: Vision-driven autonomous behavior (innate-cloud-agent)
    - LiveAgent: Real-time conversational interaction (Gemini Live API)
"""
from abc import ABC, abstractmethod
from typing import List, Optional


class Agent(ABC):
    """
    Base class for all agents.

    An agent provides personality and behavior guidelines for the robot,
    along with the list of skills that should be available when this
    agent is active.
    
    Subclasses:
        - CloudAgent: Vision-driven autonomous behavior (innate-cloud-agent)
        - LiveAgent: Real-time conversational interaction (Gemini Live API)
    """

    @property
    @abstractmethod
    def id(self) -> str:
        """
        The name of the agent (used as identifier).
        Must be defined by every subclass.
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """
        The human-readable display name of the agent.
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

    def get_prompt(self) -> Optional[str]:
        """
        Returns the prompt/description for this agent.
        This defines the robot's personality and behavior guidelines.
        
        For CloudAgent: Required - sent to cloud-agent VLM.
        For LiveAgent: Returns None (uses get_system_instruction instead).
        """
        return None

    @property
    def display_icon(self) -> Optional[str]:
        """
        Optional path to a 32x32 pixel icon asset for this agent.

        Subclasses can override this property to specify an icon.
        Default: return None (no icon).

        Example:
            return "assets/my_agent_icon.png"
        """
        return None

    def get_inputs(self) -> List[str]:
        """
        Returns a list of input device names that should be active
        when this agent is running.

        Subclasses can override this method to specify required inputs.
        Default: return empty list (no input devices required).

        Example:
            return ["micro", "camera"]
        """
        return []


class CloudAgent(Agent):
    """
    Vision-driven autonomous agent.
    
    Uses innate-cloud-agent: periodic images → VLM → skill selection.
    Robot acts autonomously based on visual input and prompt.
    
    Examples: Security patrol, autonomous exploration, task execution.
    """
    
    @abstractmethod
    def get_prompt(self) -> str:
        """
        System prompt defining robot personality and behavior.
        Sent to cloud-agent VLM for autonomous decision-making.
        
        Must be implemented by CloudAgent subclasses.
        """
        pass


class LiveAgent(Agent):
    """
    Real-time conversational agent.
    
    Uses Gemini Live API: streaming audio + periodic vision.
    Human-robot conversation with natural voice interaction.
    
    Examples: Voice assistant, tour guide, interactive companion.
    """
    
    @abstractmethod
    def get_system_instruction(self) -> str:
        """
        System instruction for Gemini Live API.
        Defines robot personality and conversational style.
        
        Must be implemented by LiveAgent subclasses.
        """
        pass
    
    @abstractmethod
    def get_proactive_prompt(self) -> str:
        """
        Prompt sent when user is silent for too long.
        Encourages robot to initiate conversation or take action.
        
        Must be implemented by LiveAgent subclasses.
        """
        pass
    
    def get_proactive_timeout(self) -> float:
        """
        Seconds of user silence before going proactive.
        
        Override to customize. Default: 15.0 seconds.
        """
        return 15.0
    
    def get_image_interval(self) -> float:
        """
        Seconds between image updates sent to Gemini.
        
        Override to customize. Default: 3.0 seconds.
        """
        return 3.0
    
    def get_gaze_enabled(self) -> bool:
        """
        Whether to enable autonomous gaze tracking (face detection + head/wheel follow).
        
        Override to customize. Default: True.
        """
        return True
    
    def get_prompt(self) -> Optional[str]:
        """
        Not used for LiveAgent - returns None.
        LiveAgent uses get_system_instruction() instead.
        """
        return None
