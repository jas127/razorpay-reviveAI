import os
from abc import ABC, abstractmethod
from pathlib import Path

try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()
except ImportError:
    pass


class ProviderError(RuntimeError):
    """Raised when an LLM provider cannot return a diagnosis response."""


class LLMProvider(ABC):
    """Small provider contract used by the provider-agnostic agent."""

    name = "unknown"

    @abstractmethod
    def generate_diagnosis(self, prompt: str) -> str:
        """Return the provider's raw text response for a diagnosis prompt."""
        raise NotImplementedError