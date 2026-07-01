# -*- coding: utf-8 -*-
"""Tests for the reconciliation discrepancy-context data gathering.

Shopify is not contacted (``fetch_shopify=False``); these exercise the Odoo-side
evidence collection the agent reasons over.
"""

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
