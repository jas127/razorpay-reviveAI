"""Gemini implementation of the ReviveAI LLMProvider interface."""

from __future__ import annotations

import os

try:
    from .base_provider import LLMProvider, ProviderError
except ImportError:  # Supports `python diagnosis/run_diagnosis.py`.
    from base_provider import LLMProvider, ProviderError


class GeminiProvider(LLMProvider):
    """Call Gemini with the fast, free-tier-friendly model from the plan."""

    name = "gemini"
    model_name = "gemini-1.5-flash"

    def generate_diagnosis(self, prompt: str) -> str:
        try:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ProviderError("GEMINI_API_KEY is not configured")

            import google.generativeai as genai

            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(self.model_name)
            response = model.generate_content(prompt)
            response_text = getattr(response, "text", None)
            if not response_text:
                raise ProviderError("Gemini returned an empty response")
            return str(response_text)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc