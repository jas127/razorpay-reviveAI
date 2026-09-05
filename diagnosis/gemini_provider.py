"""Gemini implementation of the ReviveAI LLMProvider interface."""

from __future__ import annotations

import os

try:
    from .base_provider import LLMProvider, ProviderError
except ImportError:  # Supports `python diagnosis/run_diagnosis.py`.
    from base_provider import LLMProvider, ProviderError


class GeminiProvider(LLMProvider):
    """Call Gemini with current production models."""

    name = "gemini"
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    def generate_diagnosis(self, prompt: str) -> str:
        try:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ProviderError("GEMINI_API_KEY is not configured")

            import google.generativeai as genai

            genai.configure(api_key=api_key)
            models_to_try = [self.model_name]
            for fallback in ("gemini-3.6-flash", "gemini-3.7-flash", "gemini-flash-latest"):
                if fallback not in models_to_try:
                    models_to_try.append(fallback)

            last_exc = None
            for model_id in models_to_try:
                try:
                    model = genai.GenerativeModel(model_id)
                    response = model.generate_content(prompt, request_options={"timeout": 6.0})
                    response_text = getattr(response, "text", None)
                    if response_text:
                        return str(response_text)
                except Exception as exc:
                    last_exc = exc
                    continue

            raise ProviderError(f"Gemini request failed across attempted models: {last_exc}")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc