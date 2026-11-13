"""Build the configured LLM client from settings."""

from __future__ import annotations

from text2sql.config import Settings
from text2sql.llm.cache import CachedLLMClient
from text2sql.llm.client import AnthropicClient, LLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    """Anthropic client, wrapped in the local cache when LLM_CACHE_ENABLED is true."""
    api_key, model = settings.require_llm()
    client: LLMClient = AnthropicClient(
        api_key=api_key,
        model=model,
        effort=settings.llm_effort,
        max_tokens=settings.llm_max_tokens,
        price_input_per_mtok=settings.llm_price_input_per_mtok,
        price_output_per_mtok=settings.llm_price_output_per_mtok,
    )
    if settings.llm_cache_enabled:
        client = CachedLLMClient(client, settings.llm_cache_path)
    return client
