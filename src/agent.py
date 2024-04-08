from abc import ABC, abstractmethod
from typing import Any

class Agent(ABC):

    @property
    @abstractmethod
    def agent_tag(self) -> str:
        """A unique identifier for this agent instance (used for prefix in log)."""
        pass

    @property
    @abstractmethod
    def agent_id(self) -> str:
        """The agent type id."""
        pass

    @property
    @abstractmethod
    def primary_model_id(self) -> str:
        """The unique identifier for the primary model used by the agent."""
        pass

    @property
    @abstractmethod
    def secondary_model_id(self) -> str:
        """
        The unique identifier for the secondary model used by the agent. 
        If no secondary model this matches the primary model id.
        """
        pass
