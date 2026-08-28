"""Provider interface and shared errors for the ReviveAI diagnosis layer."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    """Raised when an LLM provider cannot return a diagnosis response."""


class LLMProvider(ABC):
    """Small provider contract used by the provider-agnostic agent."""

    name = "unknown"

    @abstractmethod
    def generate_diagnosis(self, prompt: str) -> str:
        """Return the provider's raw text response for a diagnosis prompt."""
        raise NotImplementedError