# -*- coding: utf-8 -*-
"""Tests for the Shopify ``orders/create`` -> ``sale.order`` intake.

Exercises the mapping/creation rules only; no network is involved. Tagged
``post_install`` so it runs against a fully installed registry (needs ``sale``).
"""

import json

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ai_ops")
class TestOrderIntake(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Intake = cls.env["ai.ops.order.intake"]
        cls.SaleOrder = cls.env["sale.order"]

    def _payload(self, order_id="9001", sku="SKU-A", email="buyer@example.com"):
        return {
            "id": order_id,
            "name": "#%s" % order_id,
            "currency": "USD",
            "total_price": "42.00",
            "email": email,
            "customer": {"first_name": "Bob", "last_name": "Norman", "email": email},
            "shipping_address": {
                "address1": "1 Test St",
                "city": "Ottawa",
                "zip": "K1A0B1",
                "country_code": "CA",
            },
            "line_items": [
                {"title": "Widget", "sku": sku, "quantity": 2, "price": "21.00"},
            ],
        }

    def test_order_create_builds_confirmed_sale_order(self):
        result = self.Intake.process_order_create(self._payload())
        self.assertEqual(result["action"], "created")
        order = self.SaleOrder.browse(result["sale_order_id"])
        self.assertEqual(order.shopify_order_id, "9001")
        self.assertEqual(order.shopify_order_name, "#9001")
        self.assertEqual(order.state, "sale")  # confirmed
        # Prices come straight from Shopify, taxes cleared -> totals match.
        self.assertEqual(len(order.order_line), 1)
        line = order.order_line
        self.assertEqual(line.product_uom_qty, 2)
        self.assertEqual(line.price_unit, 21.0)
        self.assertFalse(line.tax_ids)
        self.assertEqual(order.amount_total, 42.0)
        # The full payload is stored verbatim.
        self.assertEqual(json.loads(order.shopify_raw_payload)["id"], "9001")

    def test_order_create_is_idempotent(self):
        first = self.Intake.process_order_create(self._payload(order_id="9002"))
        second = self.Intake.process_order_create(self._payload(order_id="9002"))
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(second["sale_order_id"], first["sale_order_id"])
        self.assertEqual(len(self.SaleOrder.search([("shopify_order_id", "=", "9002")])), 1)

    def test_unknown_sku_autocreates_product(self):
        Product = self.env["product.product"]
        self.assertFalse(Product.search([("default_code", "=", "NEW-SKU-XYZ")]))
        result = self.Intake.process_order_create(self._payload(order_id="9003", sku="NEW-SKU-XYZ"))
        order = self.SaleOrder.browse(result["sale_order_id"])
        product = order.order_line.product_id
        self.assertEqual(product.default_code, "NEW-SKU-XYZ")
        self.assertEqual(product.name, "Widget")

    def test_existing_sku_is_reused(self):
        product = self.env["product.product"].create(
            {"name": "Known Widget", "default_code": "SKU-KNOWN", "type": "consu"}
        )
        result = self.Intake.process_order_create(self._payload(order_id="9004", sku="SKU-KNOWN"))
        order = self.SaleOrder.browse(result["sale_order_id"])
        self.assertEqual(order.order_line.product_id, product)

    def test_partner_matched_by_email(self):
        partner = self.env["res.partner"].create({"name": "Returning Buyer", "email": "repeat@example.com"})
        result = self.Intake.process_order_create(self._payload(order_id="9005", email="repeat@example.com"))
        order = self.SaleOrder.browse(result["sale_order_id"])
        self.assertEqual(order.partner_id, partner)
