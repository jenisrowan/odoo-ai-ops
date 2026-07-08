# -*- coding: utf-8 -*-
"""Tests for the reconciliation discrepancy-context data gathering.

Shopify is not contacted (``fetch_shopify=False``); these exercise the Odoo-side
evidence collection the agent reasons over.
"""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ai_ops")
class TestDiscrepancyContext(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Inventory = cls.env["ai.ops.inventory"]
        cls.product = cls.env["product.product"].create(
            {"name": "AI Ops Test Widget", "is_storable": True, "default_code": "AIOPS-W1"}
        )
        cls.stock_loc = cls.env.ref("stock.stock_location_stock")
        cls.customer_loc = cls.env.ref("stock.stock_location_customers")

    def test_context_has_expected_shape(self):
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        self.assertEqual(ctx["product"]["id"], self.product.id)
        self.assertEqual(ctx["product"]["sku"], "AIOPS-W1")
        # Shopify lookup disabled -> no remote value, no discrepancy computed.
        self.assertIsNone(ctx["shopify_available"])
        self.assertIsNone(ctx["discrepancy_odoo_minus_shopify"])
        for key in (
            "odoo_on_hand",
            "pending_outgoing_moves",
            "pending_incoming_moves",
            "recent_sale_orders",
        ):
            self.assertIn(key, ctx)

    def test_agent_write_requires_approved_task(self):
        """The agent user cannot reach the inventory write paths without a
        persisted *approve* decision on a matching reconciliation task."""
        agent_user = self.env["res.users"].create(
            {
                "name": "AI Ops Agent (test)",
                "login": "ai_ops_agent_test",
                "group_ids": [(4, self.env.ref("odoo_ai_ops.group_ai_ops_agent").id)],
            }
        )
        Inventory = self.Inventory.with_user(agent_user)

        # No task reference at all -> refused.
        with self.assertRaises(UserError):
            Inventory.apply_inventory_patch(self.product.id, 5.0)

        # Task exists but carries no approve decision -> refused.
        task = self.env["ai.ops.task"].create(
            {
                "task_type": "reconciliation",
                "product_id": self.product.id,
                "state": "pending_approval",
            }
        )
        with self.assertRaises(UserError):
            Inventory.apply_inventory_patch(self.product.id, 5.0, task_id=task.id)
        with self.assertRaises(UserError):
            Inventory.push_inventory_to_shopify(self.product.id, 5.0, task_id=task.id)

        # Approved task for a *different* product -> refused.
        other = self.env["product.product"].create({"name": "Other Widget", "is_storable": True})
        task.write({"decision": "approve"})
        with self.assertRaises(UserError):
            Inventory.apply_inventory_patch(other.id, 5.0, task_id=task.id)

        # Approved task for the right product -> the write goes through.
        result = Inventory.apply_inventory_patch(self.product.id, 5.0, task_id=task.id)
        self.assertEqual(result["counted_qty"], 5.0)

    def test_open_outgoing_move_is_surfaced(self):
        # A draft outgoing move = a delivery that has NOT decremented on-hand yet;
        # exactly the "stuck move" evidence the agent needs.
        move = self.env["stock.move"].create(
            {
                "product_id": self.product.id,
                "product_uom_qty": 5.0,
                "product_uom": self.product.uom_id.id,
                "location_id": self.stock_loc.id,
                "location_dest_id": self.customer_loc.id,
            }
        )
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        surfaced = {m["id"] for m in ctx["pending_outgoing_moves"]}
        self.assertIn(move.id, surfaced)
