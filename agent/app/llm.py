"""Anthropic Claude model factory.

Risk-tiered model selection mirrors the cost model in ``cost_analysis.txt``:
medium-risk orders get a fast/cheap Haiku pre-screen, high-risk orders get a
Sonnet deep-dive.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_anthropic import ChatAnthropic

from .config import get_settings


def model_for_risk(risk_level: str) -> str:
    settings = get_settings()
    return settings.model_high if risk_level == "high" else settings.model_medium


@lru_cache
def get_chat_model(model_name: str) -> ChatAnthropic:
    """Return a cached ChatAnthropic client for ``model_name``."""
    settings = get_settings()
    return ChatAnthropic(
        model=model_name,
        api_key=settings.anthropic_api_key,
        max_tokens=settings.llm_max_tokens,
        timeout=60,
        max_retries=2,
    )
