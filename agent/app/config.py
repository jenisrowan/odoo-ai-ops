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
    # Without a socket timeout a stalled connection never returns: the awaiting
    # coroutine parks forever and the worker is wedged with no error to act on.
    # Checkpoint reads/writes are millisecond operations, so seconds here are
    # already generous; the point is that a stall surfaces as a TimeoutError the
    # caller can retry (SQS redelivers) instead of an invisible hang.
    valkey_socket_timeout: float = Field(
        default=10.0, description="Seconds to wait on a Valkey read/write before failing."
    )
    valkey_connect_timeout: float = Field(
        default=5.0, description="Seconds to wait for a Valkey connection to establish."
    )
    valkey_health_check_interval: float = Field(
        default=30.0, description="Seconds between idle-connection health pings (0 disables)."
    )
    # How long a paused workflow stays resumable. A fraud or reconciliation run
    # sits at the human-approval interrupt until a manager clicks Approve/Reject,
    # so this is effectively the manager's deadline: 5 days, then the checkpoint
    # expires and `resume()` reports the thread as unknown rather than acting on
    # a stale decision. Applied on write, so the clock runs from the pause.
    checkpoint_ttl_minutes: int = Field(
        default=5 * 24 * 60, description="Valkey TTL for checkpoints, in minutes (default 5 days)."
    )

    # --- Slack ---
    slack_bot_token: str = Field(default="")
    slack_signing_secret: str = Field(default="")
    # No default channel on purpose: a baked-in "#fraud-review" silently posts
    # approval cards to whatever that resolves to in someone else's workspace.
    # It must be supplied explicitly (SLACK_CHANNEL); Slack stays disabled otherwise.
    slack_channel: str = Field(default="", description="Target channel, e.g. #fraud-review.")

    # --- Langfuse (self-hosted telemetry) ---
    langfuse_host: str = Field(default="")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def slack_enabled(self) -> bool:
        # Both are required: a token without a channel would fail at post time.
        return bool(self.slack_bot_token and self.slack_channel)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
