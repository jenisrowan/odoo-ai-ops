# -*- coding: utf-8 -*-
"""``sale.order`` extension - the Shopify order brought into Odoo.

The AI Ops module used to be a pure gatekeeper: it never created an Odoo order,
and assumed a separate Shopify Connector imported the ``sale.order`` in parallel.
That assumption is gone. When Shopify fires ``orders/create`` we now build a
*confirmed* ``sale.order`` here (see ``ai.ops.order.intake``), so the fraud
verdict that arrives later on ``orders/risk_assessment_changed`` has a real order
to cancel.

The full, unmodified Shopify payload is stored on ``shopify_raw_payload`` - it is
the source of truth for anything we did not map into structured fields (tax
breakdown, addresses, discounts, gateway, …) and must never be lost.
"""

from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    shopify_order_id = fields.Char(
        string="Shopify Order ID",
        index=True,
        copy=False,
        help="Numeric/GID identifier of the originating Shopify order. Used to "
        "correlate the later order-risk assessment back to this order.",
    )
    shopify_order_name = fields.Char(
        string="Shopify Order Name",
        copy=False,
        help="Human-facing Shopify order reference (e.g. #1001).",
    )
    shopify_raw_payload = fields.Text(
        string="Shopify Raw Payload",
        copy=False,
        help="The entire Shopify webhook payload as received, stored verbatim. "
        "Source of truth for fields not mapped into Odoo.",
    )
    shopify_risk_level = fields.Selection(
        [
            ("none", "None"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
        ],
        string="Shopify Risk Level",
        copy=False,
        tracking=True,
        help="Latest fraud risk level reported by Shopify for this order.",
    )
    ai_ops_task_ids = fields.One2many(
        "ai.ops.task",
        "sale_order_id",
        string="AI Ops Tasks",
        help="Fraud-validation tasks opened for this order.",
    )
    ai_ops_task_count = fields.Integer(
        string="AI Ops Task Count",
        compute="_compute_ai_ops_task_count",
    )

    def _compute_ai_ops_task_count(self):
        for order in self:
            order.ai_ops_task_count = len(order.ai_ops_task_ids)

    def action_view_ai_ops_tasks(self):
        """Open the fraud tasks opened for this order (smart button)."""
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("odoo_ai_ops.action_ai_ops_task")
        action["domain"] = [("sale_order_id", "=", self.id)]
        action["context"] = {"default_sale_order_id": self.id, "search_default_sale_order_id": self.id}
        return action
