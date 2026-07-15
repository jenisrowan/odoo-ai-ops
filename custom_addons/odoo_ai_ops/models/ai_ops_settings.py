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

from odoo import _, api, fields, models
from odoo.addons.odoo_ai_ops.services.shopify_client import ShopifyClient, ShopifyError
from odoo.exceptions import UserError

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
    "odoo_ai_ops.shopify_webhook_callback_url": "SHOPIFY_WEBHOOK_CALLBACK_URL",
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
    # NOTE: the agent shared token is a secret and is deliberately NOT a field
    # here. It is the same value both Odoo and the FastAPI agent read from AWS
    # Secrets Manager (odoo/integration/credentials -> key `ai_ops_shared_token`,
    # injected as AI_OPS_SHARED_TOKEN into both tasks). A UI-editable copy could
    # be changed in Odoo alone and would then no longer match the agent, breaking
    # authentication. Resolved at runtime via _ai_ops_get_param -> os.environ.
    ai_ops_shopify_shop_domain = fields.Char(
        string="Shopify Shop Domain",
        config_parameter="odoo_ai_ops.shopify_shop_domain",
        help="e.g. my-store.myshopify.com",
    )
    # NOTE: the Shopify Admin API token is a secret and is deliberately NOT a
    # field here. It lives once in AWS Secrets Manager (odoo/integration/
    # credentials -> key `shopify_admin_token`) and is injected as the
    # SHOPIFY_ADMIN_TOKEN env var (templates/odoo-task.json / .env). Exposing it
    # as a UI-editable config_parameter would duplicate the secret into the
    # database. Resolved at runtime via _ai_ops_get_param -> os.environ, same as
    # `shopify_webhook_secret`.
    ai_ops_shopify_api_version = fields.Char(
        string="Shopify API Version",
        config_parameter="odoo_ai_ops.shopify_api_version",
        default=DEFAULT_API_VERSION,
    )
    ai_ops_shopify_webhook_callback_url = fields.Char(
        string="Shopify Webhook Callback URL",
        config_parameter="odoo_ai_ops.shopify_webhook_callback_url",
        help="Public HTTPS endpoint Shopify posts webhooks to. This is the edge "
        "ingress, not Odoo directly: https://<cloudfront-domain>/webhooks/shopify "
        "(Terraform output `webhook_url` + /shopify). The 'Register Shopify "
        "Webhooks' button subscribes orders/create and "
        "orders/risk_assessment_changed to this URL via the Admin API.",
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

    def _ai_ops_resolve(self, field_name, key, default=None):
        """Resolve a NON-SECRET value, preferring the (possibly unsaved) form field.

        Lets the 'Register Shopify Webhooks' button use a shop domain / callback
        URL just typed into the form before saving, while still falling back to
        the stored parameter / env. Do not use this for secrets: they are never
        UI fields, so resolve those with _ai_ops_get_param (env / Secrets Manager).
        """
        self.ensure_one()
        return self[field_name] or self._ai_ops_get_param(key, default=default)

    def action_ai_ops_register_shopify_webhooks(self):
        """Subscribe the AI Ops Shopify topics to the edge webhook URL.

        Shopify's UI can't create plain HTTPS webhooks for a custom app, so we
        register them through the Admin API. Idempotent: re-points or skips
        existing subscriptions. Surfaced as a Settings button rather than a
        one-off script so the destination is versioned config, not tribal
        knowledge.
        """
        self.ensure_one()
        callback_url = self._ai_ops_resolve(
            "ai_ops_shopify_webhook_callback_url",
            "odoo_ai_ops.shopify_webhook_callback_url",
        )
        if not callback_url:
            raise UserError(
                _(
                    "Set the Shopify Webhook Callback URL first, e.g. "
                    "https://<cloudfront-domain>/webhooks/shopify"
                )
            )
        try:
            client = ShopifyClient(
                shop_domain=self._ai_ops_resolve(
                    "ai_ops_shopify_shop_domain", "odoo_ai_ops.shopify_shop_domain"
                ),
                # Secret: from Secrets Manager / SHOPIFY_ADMIN_TOKEN, never the UI.
                admin_token=self._ai_ops_get_param("odoo_ai_ops.shopify_admin_token"),
                api_version=self._ai_ops_resolve(
                    "ai_ops_shopify_api_version",
                    "odoo_ai_ops.shopify_api_version",
                    DEFAULT_API_VERSION,
                ),
            )
            summary = client.sync_webhooks(callback_url)
        except ShopifyError as exc:
            raise UserError(_("Shopify webhook registration failed: %s") % exc) from exc

        lines = [
            "%s: %s" % (label, ", ".join(summary[bucket]))
            for label, bucket in (
                (_("Created"), "created"),
                (_("Updated"), "updated"),
                (_("Already up to date"), "unchanged"),
            )
            if summary.get(bucket)
        ]
        _logger.info("AI Ops: synced Shopify webhooks to %s -> %s", callback_url, summary)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Shopify Webhooks Registered"),
                "message": _("Target: %s\n%s") % (callback_url, "\n".join(lines)),
                "type": "success",
                "sticky": False,
            },
        }
