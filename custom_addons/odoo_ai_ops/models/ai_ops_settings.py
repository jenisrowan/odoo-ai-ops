# -*- coding: utf-8 -*-
"""Central configuration for the AI Ops integration.

All tunables (agent endpoint, shared auth token, Shopify credentials, the
auto-rejection threshold, …) live in ``ir.config_parameter`` so they can be
managed from the Settings UI *and* seeded from the container environment.

On ECS the Odoo task receives these values as environment variables / Secrets
Manager bindings (see ``templates/odoo-task.json``). To avoid persisting
secrets such as the Shopify Admin token in the database, the read helpers fall
back to ``os.environ`` whenever the stored parameter is empty. This keeps the
``odoo/integration/credentials`` secret as the single source of truth at
runtime while still allowing a value to be overridden from the UI for local
development.
"""

import logging
import os

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Mapping: ir.config_parameter key -> environment variable used as a fallback.
# The environment variables are injected by the ECS task definition.
PARAM_ENV_FALLBACK = {
    "odoo_ai_ops.agent_base_url": "AGENT_BASE_URL",
    "odoo_ai_ops.shared_token": "AI_OPS_SHARED_TOKEN",
    "odoo_ai_ops.shopify_shop_domain": "SHOPIFY_SHOP_DOMAIN",
    "odoo_ai_ops.shopify_admin_token": "SHOPIFY_ADMIN_TOKEN",
    "odoo_ai_ops.shopify_api_version": "SHOPIFY_API_VERSION",
    "odoo_ai_ops.shopify_webhook_secret": "SHOPIFY_WEBHOOK_SECRET",
}

DEFAULT_API_VERSION = "2026-07"
DEFAULT_BYPASS_THRESHOLD = 10.0


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_ops_agent_base_url = fields.Char(
        string="Agent Base URL",
        config_parameter="odoo_ai_ops.agent_base_url",
        help="Base URL of the FastAPI + LangGraph agent cluster, e.g. "
        "http://fastapi.odoo.local:8000 (reached via ECS Service Connect).",
    )
    ai_ops_shared_token = fields.Char(
        string="Agent Shared Token",
        config_parameter="odoo_ai_ops.shared_token",
        help="Bearer token shared between Odoo and the agent. Used to "
        "authenticate forwarded webhooks and agent callbacks.",
    )
    ai_ops_shopify_shop_domain = fields.Char(
        string="Shopify Shop Domain",
        config_parameter="odoo_ai_ops.shopify_shop_domain",
        help="e.g. my-store.myshopify.com",
    )
    ai_ops_shopify_admin_token = fields.Char(
        string="Shopify Admin API Token",
        config_parameter="odoo_ai_ops.shopify_admin_token",
    )
    ai_ops_shopify_api_version = fields.Char(
        string="Shopify API Version",
        config_parameter="odoo_ai_ops.shopify_api_version",
        default=DEFAULT_API_VERSION,
    )
    ai_ops_bypass_threshold = fields.Float(
        string="Auto-Reject Threshold",
        config_parameter="odoo_ai_ops.bypass_threshold",
        default=DEFAULT_BYPASS_THRESHOLD,
        help="Orders strictly cheaper than this amount that are flagged "
        "medium/high risk are cancelled directly in Shopify without "
        "spending any LLM tokens.",
    )
    ai_ops_auto_reject_enabled = fields.Boolean(
        string="Enable Cheap-Order Auto-Rejection",
        config_parameter="odoo_ai_ops.auto_reject_enabled",
        default=True,
    )
    ai_ops_refund_on_cancel = fields.Boolean(
        string="Refund When Cancelling in Shopify",
        config_parameter="odoo_ai_ops.refund_on_cancel",
        default=False,
        help="If enabled, Shopify order cancellations issued by AI Ops "
        "(auto-reject and manager rejections) also refund the payment. "
        "Disabled by default: fraud rejections usually should not "
        "auto-refund — void or review the payment instead.",
    )

    @api.model
    def _ai_ops_get_param(self, key, default=None):
        """Read a configuration value, falling back to the environment.

        The database (``ir.config_parameter``) wins when it holds a non-empty
        value; otherwise we look up the mapped environment variable. This lets
        the same code path serve both UI-managed local installs and
        secret-injected ECS deployments.
        """
        value = self.env["ir.config_parameter"].sudo().get_param(key)
        if value:
            return value
        env_key = PARAM_ENV_FALLBACK.get(key)
        if env_key:
            env_value = os.environ.get(env_key)
            if env_value:
                return env_value
        return default
