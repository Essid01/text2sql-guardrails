"""Application settings, loaded from environment variables (and `.env` if present)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Typed configuration. Every value comes from the environment, never from code."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    anthropic_api_key: SecretStr | None = None
    llm_model: str | None = None
    # Thinking depth / token spend. "low" = cheapest; the model may skip thinking entirely.
    llm_effort: Effort = "low"
    # Thinking tokens count toward this cap, so keep headroom above the expected answer size.
    llm_max_tokens: int = Field(default=4096, gt=0)
    llm_cache_enabled: bool = True
    llm_cache_path: str = ".cache/llm_cache.sqlite"
    # USD per million tokens, used only for cost reporting in the evaluation.
    llm_price_input_per_mtok: float = Field(default=0.0, ge=0)
    llm_price_output_per_mtok: float = Field(default=0.0, ge=0)

    # Database
    database_url: str = "postgresql+psycopg://text2sql:text2sql@localhost:5432/retail"

    # Schema retrieval. None = send the full schema (measured better on our 7 tables).
    schema_top_k: int | None = Field(default=None, gt=0)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Logging
    log_level: str = "INFO"

    def require_llm(self) -> tuple[str, str]:
        """Return (api_key, model) or raise a clear error if the LLM is not configured."""
        if self.anthropic_api_key is None or not self.anthropic_api_key.get_secret_value():
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example).")
        if not self.llm_model:
            raise RuntimeError("LLM_MODEL is not set (see .env.example).")
        return self.anthropic_api_key.get_secret_value(), self.llm_model

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate the USD cost of a call from token usage and configured prices."""
        return (
            input_tokens * self.llm_price_input_per_mtok
            + output_tokens * self.llm_price_output_per_mtok
        ) / 1_000_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
