"""Runtime configuration for the agent service.

All values are sourced from environment variables (12-factor). On ECS these are
provided by the task definition: plain values as ``environment`` entries and
secrets as ``secrets`` bindings resolved from AWS Secrets Manager at start-up.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Service ---
    log_level: str = Field(default="INFO")
    enable_sqs_worker: bool = Field(default=True, description="Run the SQS poller loop.")

    # --- AWS / SQS ---
    aws_region: str = Field(default="ap-south-1")
    sqs_queue_url: str = Field(default="")
    sqs_wait_time_seconds: int = Field(default=20)  # long polling
    sqs_max_messages: int = Field(default=10)
    sqs_visibility_timeout: int = Field(default=120)

    # --- Odoo (JSON-RPC + webhook forwarding) ---
    odoo_base_url: str = Field(default="http://odoo.odoo.local:8069")
    odoo_db: str = Field(default="odoo")
    odoo_username: str = Field(default="")
    odoo_password: str = Field(default="")
    ai_ops_shared_token: str = Field(default="")

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="")
    model_medium: str = Field(default="claude-haiku-4-5-20251001")
    model_high: str = Field(default="claude-sonnet-5")
    llm_max_tokens: int = Field(default=1024)

    # --- Valkey / Redis (LangGraph checkpoints + telemetry buffer) ---
    valkey_url: str = Field(default="", description="rediss://host:port URL.")

    # --- Slack ---
    slack_bot_token: str = Field(default="")
    slack_signing_secret: str = Field(default="")
    slack_channel: str = Field(default="#fraud-review")

    # --- Langfuse (self-hosted telemetry) ---
    langfuse_host: str = Field(default="")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
