from abc import ABC, abstractmethod


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
        Returns a tuple of (result_message, success_flag)
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

    def guidelines(self):
        """
        Optionally provide guidelines for this primitive.
        Subclasses may override this method if guidelines are available.
        """
        return None
