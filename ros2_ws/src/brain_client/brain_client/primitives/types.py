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
        """
        pass

    def guidelines(self):
        """
        Optionally provide guidelines for this primitive.
        Subclasses may override this method if guidelines are available.
        """
        return None
