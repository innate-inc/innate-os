from abc import ABC, abstractmethod
from enum import Enum
from rclpy.node import Node


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
    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"


class Primitive(ABC):
    def __init__(self, logger):
        self.logger = logger
        self.node: Node | None = None
        self._feedback_callback = None

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
    
    def guidelines_when_running(self):
        """
        Optionally provide guidelines for this primitive when it is running.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def set_feedback_callback(self, callback):
        """Sets the feedback callback function."""
        self._feedback_callback = callback
        if self.logger:  # Check if logger is available
            self.logger.debug(f"Feedback callback set for primitive {self.name}.")
        else:
            # Consider a simple print or no log if logger isn't guaranteed
            print(
                f"DEBUG: Feedback callback set for primitive {self.name} (logger not available)."
            )

    def _send_feedback(self, message: str):
        """Sends feedback if the callback is set."""
        if self._feedback_callback:
            try:
                self._feedback_callback(message)
            except Exception as e:
                if self.logger:
                    self.logger.error(
                        f"Error sending feedback for primitive {self.name}: {e}"
                    )
                else:
                    print(
                        f"ERROR: Error sending feedback for primitive {self.name}: {e} (logger not available)."
                    )
        # else:
        # Optionally log if feedback is not sent because callback is not set
        # if self.logger:
        #     self.logger.debug(f"Feedback callback not set for {self.name}. Message not sent: {message}")
        # else:
        #     print(f"DEBUG: Feedback callback not set for {self.name}. Message not sent: {message} (logger not available).")
