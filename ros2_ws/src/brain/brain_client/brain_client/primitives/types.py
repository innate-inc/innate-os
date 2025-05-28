from abc import ABC, abstractmethod
from enum import Enum


class PrimitiveResult(Enum):
    """
    Enum representing the possible results of a primitive execution.
    """

    SUCCESS = "success"  # The primitive completed successfully
    FAILURE = "failure"  # The primitive failed to complete
    CANCELLED = "cancelled"  # The primitive was cancelled before completion


class RobotStateType(Enum):
    """
    Enum representing the types of robot state a primitive might require.
    """

    LAST_IMAGE_B64 = "last_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    # Add more states as needed, e.g., LAST_DEPTH_IMAGE_B64


class Primitive(ABC):
    @property
    @abstractmethod
    def name(self):
        """
        The name of the primitive.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def execute(self, *args, **kwargs):
        """
        Execute the primitive.

        Subclasses must implement this method.
        Returns a tuple of (result_message, result_status) where result_status
        is a PrimitiveResult enum value.
        """
        pass

    @abstractmethod
    def cancel(self):
        """
        Cancel the execution of the primitive.

        Subclasses must implement this method to properly handle cancellation.
        This method should be safe to call at any time, even if the primitive
        is not currently executing.

        Returns a message describing the cancellation result.
        """
        pass

    def update_robot_state(self, **kwargs):
        """
        Update the primitive with the latest robot state.
        Subclasses can override this to store relevant data.
        Example: self.last_image = kwargs.get('last_image_b64')
        """
        pass

    def get_required_robot_states(self) -> list[RobotStateType]:
        """
        Declare the robot states required by this primitive.
        Subclasses should override this to specify their needs.
        Returns a list of RobotStateType enum members.
        """
        return []

    def guidelines(self):
        """
        Optionally provide guidelines for this primitive.
        Subclasses may override this method if guidelines are available.
        """
        return None
