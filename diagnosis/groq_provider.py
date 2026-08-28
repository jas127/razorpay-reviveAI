"""Groq implementation of the ReviveAI LLMProvider interface."""

from __future__ import annotations

import os

try:
    from .base_provider import LLMProvider, ProviderError
except ImportError:  # Supports `python diagnosis/run_diagnosis.py`.
    from base_provider import LLMProvider, ProviderError


class GroqProvider(LLMProvider):
    """Call Groq's OpenAI-compatible chat completion API."""

    name = "groq"
    model_name = "llama-3.3-70b-versatile"

    def generate_diagnosis(self, prompt: str) -> str:
        try:
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise ProviderError("GROQ_API_KEY is not configured")

            from groq import Groq

            client = Groq(api_key=api_key)
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
            )
            response_text = response.choices[0].message.content
            if not response_text:
                raise ProviderError("Groq returned an empty response")
            return str(response_text)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Groq request failed: {exc}") from exc