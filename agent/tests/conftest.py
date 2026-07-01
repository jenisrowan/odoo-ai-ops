"""Test configuration.

Environment variables are set at import time - before the application module is
imported - so the cached :class:`Settings` singleton picks them up. This keeps
the tests fully offline (no Valkey, Slack, SQS or Anthropic required).
"""

import os

os.environ.setdefault("AI_OPS_SHARED_TOKEN", "testtoken")
os.environ.setdefault("ENABLE_SQS_WORKER", "false")
os.environ.setdefault("VALKEY_URL", "")
os.environ.setdefault("SLACK_BOT_TOKEN", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ODOO_USERNAME", "agent")
os.environ.setdefault("ODOO_PASSWORD", "agent")
