# -*- coding: utf-8 -*-
{
    "name": "Odoo AI Ops",
    "version": "19.0.1.0.0",
    "category": "Operations/AI",
    "summary": "AI-assisted fraud triage and inventory reconciliation for Odoo, "
    "driven by a FastAPI + LangGraph agent cluster.",
    "description": """
Odoo AI Ops
===========

Custom integration layer between Odoo 19 and the external FastAPI + LangGraph
agent cluster described in the project architecture.

Capabilities
------------
* **Shopify OrderRisk gatekeeper** - receives forwarded Shopify ``orders/risk``
  webhook payloads from the agent, applies a cheap-order auto-rejection rule
  (total < threshold AND medium/high risk -> cancel in Shopify, no LLM spend),
  and otherwise opens an ``ai.ops.task`` and dispatches the LangGraph fraud
  workflow over REST.
* **Reconciliation & inventory actions** - JSON-RPC callable methods the agent
  uses to query the catalog, inspect historical warehouse moves and write back
  inventory adjustment patches.
* **Human-in-the-loop approval flow** - a persistent ``ai.ops.task`` state
  machine (``pending_approval`` -> ``approved`` / ``rejected``) updated when the
  agent relays a manager's Slack decision.
""",
    "author": "Odoo AI Ops",
    "website": "https://github.com/jenisrowan/odoo-ai-ops",
    "license": "LGPL-3",
    "depends": [
        "base",
        "mail",
        "stock",
    ],
    "data": [
        "security/ai_ops_security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence.xml",
        "data/ir_config_parameter.xml",
        "data/ir_cron.xml",
        "views/ai_ops_task_views.xml",
        "views/res_config_settings_views.xml",
        "views/ai_ops_menus.xml",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "application": True,
    "installable": True,
}
