# -*- coding: utf-8 -*-
"""Tests for the reconciliation discrepancy-context data gathering.

Shopify is not contacted (``fetch_shopify=False``); these exercise the Odoo-side
evidence collection the agent reasons over.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
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

    def test_move_details_include_the_picking(self):
        """The investigation's drill-down needs the picking, not just the move.

        Move state alone cannot distinguish "shipped but never validated in
        Odoo" from "still sitting in the warehouse" - the picking can.
        """
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self.env.ref("stock.picking_type_out").id,
                "location_id": self.stock_loc.id,
                "location_dest_id": self.customer_loc.id,
            }
        )
        move = self.env["stock.move"].create(
            {
                "product_id": self.product.id,
                "product_uom_qty": 3.0,
                "product_uom": self.product.uom_id.id,
                "location_id": self.stock_loc.id,
                "location_dest_id": self.customer_loc.id,
                "picking_id": picking.id,
            }
        )
        rows = self.Inventory.move_details([move.id])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], move.id)
        self.assertEqual(rows[0]["picking"]["name"], picking.name)
        self.assertEqual(rows[0]["location_to_usage"], "customer")

    def test_manual_adjustment_is_surfaced_with_who_did_it(self):
        """"Someone forcefully changed the on-hand qty" is a first-class cause.

        Nothing else in the snapshot would reveal it, and the author is usually
        the whole answer.
        """
        self.Inventory.apply_inventory_patch(self.product.id, 42.0, reason="hand count")
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        adjustments = ctx["recent_inventory_adjustments"]
        self.assertTrue(adjustments, "a manual adjustment left no trace in the snapshot")
        self.assertEqual(adjustments[0]["kind"], "inventory_adjustment")
        self.assertEqual(adjustments[0]["user"], self.env.user.display_name)

    def test_internal_transfer_is_surfaced(self):
        """A transfer between locations leaves total on-hand unchanged."""
        other_loc = self.env["stock.location"].create(
            {"name": "AI Ops Second Shelf", "usage": "internal", "location_id": self.stock_loc.id}
        )
        move = self.env["stock.move"].create(
            {
                "product_id": self.product.id,
                "product_uom_qty": 4.0,
                "product_uom": self.product.uom_id.id,
                "location_id": self.stock_loc.id,
                "location_dest_id": other_loc.id,
            }
        )
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        internal = ctx["pending_internal_moves"]
        self.assertIn(move.id, {m["id"] for m in internal})
        self.assertEqual(
            next(m["kind"] for m in internal if m["id"] == move.id), "internal_transfer"
        )
        # It must NOT be misfiled as a delivery to a customer.
        self.assertNotIn(move.id, {m["id"] for m in ctx["pending_outgoing_moves"]})

    def test_stock_by_location_shows_where_it_sits(self):
        """The 'moved to another warehouse' evidence."""
        self.Inventory.apply_inventory_patch(self.product.id, 9.0)
        breakdown = self.Inventory.stock_by_location(self.product.id)
        self.assertEqual(breakdown["total_on_hand"], 9.0)
        locations = {row["location_id"]: row for row in breakdown["locations"]}
        self.assertIn(self.stock_loc.id, locations)
        self.assertEqual(locations[self.stock_loc.id]["quantity"], 9.0)
        self.assertEqual(locations[self.stock_loc.id]["available"], 9.0)
        # And it is part of the deterministic snapshot, not only a follow-up.
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        self.assertTrue(ctx["stock_by_location"])

    def test_moves_are_classified_by_kind(self):
        outgoing = self.env["stock.move"].create(
            {
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
                "product_uom": self.product.uom_id.id,
                "location_id": self.stock_loc.id,
                "location_dest_id": self.customer_loc.id,
            }
        )
        rows = self.Inventory.warehouse_moves(self.product.id, states=["draft"])
        self.assertEqual(
            next(r["kind"] for r in rows if r["id"] == outgoing.id), "outgoing"
        )
        # Filtering to a kind that isn't present returns nothing rather than all.
        self.assertEqual(
            self.Inventory.warehouse_moves(
                self.product.id, states=["draft"], kinds=["internal_transfer"]
            ),
            [],
        )

    def test_ledger_balances_after_a_tracked_adjustment(self):
        """A normal inventory adjustment creates a move, so the ledger balances."""
        self.Inventory.apply_inventory_patch(self.product.id, 30.0)
        check = self.Inventory.ledger_check(self.product.id)
        self.assertTrue(check["balanced"], check)
        self.assertEqual(check["actual_in_quants"], 30.0)
        self.assertNotIn("warning", check)

    def test_ledger_detects_a_corrupted_quant(self):
        """The invariant: moves are the ledger, quants are the balance.

        Reaching this state requires writing the ``quantity`` field, which is
        readonly - no operator action can do it. The check exists as a canary
        for code that does it anyway, not as a normal root cause.
        """
        self.Inventory.apply_inventory_patch(self.product.id, 30.0)
        quant = self.env["stock.quant"].search(
            [("product_id", "=", self.product.id), ("location_id", "=", self.stock_loc.id)]
        )
        quant.write({"quantity": 999.0})  # readonly field, written anyway
        self.env.flush_all()
        self.product.invalidate_recordset()

        check = self.Inventory.ledger_check(self.product.id)
        self.assertFalse(check["balanced"], check)
        self.assertEqual(check["actual_in_quants"], 999.0)
        self.assertEqual(check["expected_from_moves"], 30.0)
        self.assertEqual(check["gap"], 969.0)
        self.assertIn("DATA INCONSISTENCY", check["warning"])

    def test_ledger_does_not_cry_wolf_outside_a_warehouse_tree(self):
        """No false positive for stock in an internal location with no warehouse.

        `qty_available` only counts internal locations under a warehouse view
        location, while the move ledger counts every internal location. Using
        the two together would report a phantom gap for perfectly consistent
        data, sending a human to chase a corruption that never happened.
        """
        orphan = self.env["stock.location"].create(
            {"name": "Unattached Internal", "usage": "internal"}
        )
        self.assertFalse(orphan.warehouse_id, "fixture must be outside a warehouse tree")
        self.Inventory.apply_inventory_patch(self.product.id, 12.0, location_id=orphan.id)

        check = self.Inventory.ledger_check(self.product.id)
        self.assertTrue(check["balanced"], check)
        self.assertNotIn("warning", check)

    def test_shopify_orders_without_sku_returns_an_error_row(self):
        """No SKU means no way to match in Shopify - say so, don't raise."""
        product = self.env["product.product"].create(
            {"name": "No SKU Widget", "is_storable": True}
        )
        result = self.Inventory.shopify_orders_for_sku(product.id)
        self.assertIn("error", result)

    def test_shopify_orders_survive_a_shopify_outage(self):
        """A Shopify failure must degrade to evidence, not kill the workflow."""
        with patch.object(
            type(self.Inventory), "_shopify_client", side_effect=Exception("Shopify down")
        ):
            result = self.Inventory.shopify_orders_for_sku(self.product.id)
        self.assertIn("error", result)
        self.assertIn("Shopify down", result["error"])

    def test_move_details_tolerates_unknown_ids(self):
        # A model that hallucinates a move id should get an empty answer, not a
        # crashed workflow.
        self.assertEqual(self.Inventory.move_details([987654321]), [])
        self.assertEqual(self.Inventory.move_details([]), [])


@tagged("post_install", "-at_install", "ai_ops")
class TestShopifyLocationScope(TransactionCase):
    """Odoo's number and Shopify's number must describe the same stock.

    The shop-location + back-warehouse setup is ordinary, and Odoo's headline
    on-hand sums both. Unscoped, the standing gap reads as a Shopify undercount
    and the fix would push warehouse stock into Shopify and oversell it.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Inventory = cls.env["ai.ops.inventory"]
        cls.product = cls.env["product.product"].create(
            {"name": "Scope Widget", "is_storable": True, "default_code": "AIOPS-SCOPE"}
        )
        cls.shop = cls.env.ref("stock.stock_location_stock")
        # Must hang off the warehouse's view location: qty_available only counts
        # internal locations under a warehouse, so a root-level location would
        # be silently excluded and the fixture would not model the real setup.
        cls.warehouse_back = cls.env["stock.location"].create(
            {
                "name": "Back Warehouse",
                "usage": "internal",
                "location_id": cls.shop.location_id.id,
            }
        )
        # 5 in the shop (feeds Shopify), 100 out back (does not).
        cls.Inventory.apply_inventory_patch(cls.product.id, 5.0, location_id=cls.shop.id)
        cls.Inventory.apply_inventory_patch(
            cls.product.id, 100.0, location_id=cls.warehouse_back.id
        )

    def _set_location(self, location):
        self.env["ir.config_parameter"].sudo().set_param(
            "odoo_ai_ops.shopify_stock_location_id", str(location.id) if location else ""
        )

    def test_unconfigured_multi_location_is_flagged_not_silently_wrong(self):
        self._set_location(False)
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        scope = ctx["location_scope"]
        self.assertFalse(scope["configured"])
        self.assertEqual(scope["stocked_internal_locations"], 2)
        self.assertIn("AMBIGUOUS COMPARISON", scope["warning"])
        # The AI must be told not to "correct" Shopify off this comparison.
        self.assertIn("Do NOT recommend", scope["warning"])
        self.assertEqual(ctx["odoo_on_hand"], 105.0)

    def test_configured_location_compares_only_that_stock(self):
        self._set_location(self.shop)
        ctx = self.Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        scope = ctx["location_scope"]
        self.assertTrue(scope["configured"])
        # 5 in the shop, NOT the 105 Odoo holds in total.
        self.assertEqual(ctx["odoo_on_hand"], 5.0)
        self.assertEqual(scope["odoo_total_all_locations"], 105.0)
        self.assertNotIn("warning", scope)

    def test_single_location_needs_no_configuration(self):
        """Don't cry wolf on the simple setup."""
        self._set_location(False)
        simple = self.env["product.product"].create(
            {"name": "Simple Widget", "is_storable": True, "default_code": "AIOPS-SIMPLE"}
        )
        self.Inventory.apply_inventory_patch(simple.id, 8.0, location_id=self.shop.id)
        scope = self.Inventory.discrepancy_context(simple.id, fetch_shopify=False)[
            "location_scope"
        ]
        self.assertNotIn("warning", scope)
        self.assertEqual(scope["stocked_internal_locations"], 1)


@tagged("post_install", "-at_install", "ai_ops")
class TestAgentCredentialIsConstrained(TransactionCase):
    """The agent's Odoo credential must be powerless outside the gated methods.

    The read-only toolbelt and the approval gate both live above Odoo, so
    neither constrains what someone holding the agent's JSON-RPC password can
    do with a direct ``execute_kw``. These tests pin the boundary that does:
    no stock manager role, and record rules denying every direct write.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls.env["res.users"].create(
            {
                "name": "AI Ops Agent (credential test)",
                "login": "ai_ops_agent_cred_test",
                "group_ids": [(4, cls.env.ref("odoo_ai_ops.group_ai_ops_agent").id)],
            }
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Credential Test Widget", "is_storable": True, "default_code": "AIOPS-CRED"}
        )
        cls.stock_loc = cls.env.ref("stock.stock_location_stock")
        cls.customer_loc = cls.env.ref("stock.stock_location_customers")

    def _move(self):
        return self.env["stock.move"].create(
            {
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
                "product_uom": self.product.uom_id.id,
                "location_id": self.stock_loc.id,
                "location_dest_id": self.customer_loc.id,
            }
        )

    def test_agent_is_not_a_stock_manager(self):
        self.assertTrue(self.agent.has_group("stock.group_stock_user"))
        # Manager would carry unlink on stock.move and full control of
        # warehouses, locations, routes and putaway rules.
        self.assertFalse(self.agent.has_group("stock.group_stock_manager"))

    def test_agent_cannot_write_quants_directly(self):
        """The whole point: no path to stock except the approved-task method."""
        quant = self.env["stock.quant"].create(
            {
                "product_id": self.product.id,
                "location_id": self.stock_loc.id,
                "inventory_quantity": 0.0,
            }
        )
        with self.assertRaises(AccessError):
            quant.with_user(self.agent).write({"inventory_quantity": 999.0})
            self.env.flush_all()

    def test_agent_cannot_create_quants_directly(self):
        with self.assertRaises(AccessError):
            self.env["stock.quant"].with_user(self.agent).create(
                {
                    "product_id": self.product.id,
                    "location_id": self.stock_loc.id,
                    "inventory_quantity": 500.0,
                }
            )
            self.env.flush_all()

    def test_agent_cannot_write_or_unlink_moves(self):
        move = self._move()
        with self.assertRaises(AccessError):
            move.with_user(self.agent).write({"product_uom_qty": 99.0})
            self.env.flush_all()
        with self.assertRaises(AccessError):
            move.with_user(self.agent).unlink()

    def test_agent_can_still_read_the_evidence(self):
        """Constraining writes must not blind the investigation."""
        move = self._move()
        Inventory = self.env["ai.ops.inventory"].with_user(self.agent)
        ctx = Inventory.discrepancy_context(self.product.id, fetch_shopify=False)
        self.assertIn(move.id, {m["id"] for m in ctx["pending_outgoing_moves"]})
        self.assertTrue(Inventory.move_details([move.id]))
        self.assertTrue(
            Inventory.query_catalog(domain=[("default_code", "=", "AIOPS-CRED")])
        )

    def test_the_gated_write_path_still_works(self):
        """Losing the manager role must not break the approved adjustment.

        ``apply_inventory_patch`` elevates internally after the approval gate,
        which is what keeps this working now that the credential itself cannot
        touch a quant.
        """
        task = self.env["ai.ops.task"].create(
            {
                "task_type": "reconciliation",
                "product_id": self.product.id,
                "state": "approved",
                "decision": "approve",
            }
        )
        result = self.env["ai.ops.inventory"].with_user(self.agent).apply_inventory_patch(
            self.product.id, 12.0, task_id=task.id
        )
        self.assertEqual(result["counted_qty"], 12.0)
        self.assertEqual(
            self.product.with_context(location=self.stock_loc.id).qty_available, 12.0
        )

    def test_the_rule_does_not_touch_other_users(self):
        """A global rule applies to everyone, so prove it is a no-op elsewhere."""
        manager = self.env["res.users"].create(
            {
                "name": "Stock Manager (test)",
                "login": "ai_ops_stock_manager_test",
                "group_ids": [(4, self.env.ref("stock.group_stock_manager").id)],
            }
        )
        quant = self.env["stock.quant"].create(
            {
                "product_id": self.product.id,
                "location_id": self.stock_loc.id,
                "inventory_quantity": 0.0,
            }
        )
        quant.with_user(manager).write({"inventory_quantity": 7.0})
        self.env.flush_all()
        self.assertEqual(quant.inventory_quantity, 7.0)
