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
    model_name = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

    def generate_diagnosis(self, prompt: str) -> str:
        try:
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                raise ProviderError("GROQ_API_KEY is not configured")

            from groq import Groq

            client = Groq(api_key=api_key, max_retries=1, timeout=8.0)
            models_to_try = [self.model_name]
            for fallback in ("openai/gpt-oss-20b", "qwen/qwen3.6-27b", "openai/gpt-oss-120b"):
                if fallback not in models_to_try:
                    models_to_try.append(fallback)

            last_exc = None
            for model in models_to_try:
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.2,
                        max_tokens=500,
                    )
                    response_text = response.choices[0].message.content
                    if response_text:
                        return str(response_text)
                except Exception as exc:
                    last_exc = exc
                    continue

            raise ProviderError(f"Groq request failed across attempted models: {last_exc}")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Groq request failed: {exc}") from exc