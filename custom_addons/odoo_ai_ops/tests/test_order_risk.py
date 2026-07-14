# -*- coding: utf-8 -*-
"""Unit tests for the order-risk gatekeeper decision logic.

Network side effects (Shopify cancellation, agent dispatch) are patched out so
the tests exercise only the routing rules. Tagged ``post_install`` so they run
against a fully installed registry.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "ai_ops")
class TestOrderRiskGatekeeper(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Risk = cls.env["ai.ops.order.risk"]
        cls.Task = cls.env["ai.ops.task"]
        # Deterministic config for the assertions below.
        cls.env["ir.config_parameter"].sudo().set_param("odoo_ai_ops.bypass_threshold", "10.0")
        cls.env["ir.config_parameter"].sudo().set_param("odoo_ai_ops.auto_reject_enabled", "True")

    def _payload(self, total, risk, order_id="55501"):
        return {"order_id": order_id, "order_name": "#1001", "total": total, "currency": "USD", "risk_level": risk}

    def _make_order(self, total, order_id):
        """A draft sale.order correlated to a Shopify order id, worth ``total``."""
        product = self.env["product.product"].create({"name": "Test SKU", "type": "service"})
        partner = self.env["res.partner"].create({"name": "Risk Buyer"})
        order = self.env["sale.order"].create({"partner_id": partner.id, "shopify_order_id": order_id})
        self.env["sale.order.line"].create({"order_id": order.id, "product_id": product.id, "product_uom_qty": 1})
        order.order_line.write({"price_unit": total, "tax_ids": [(5, 0, 0)]})
        return order

    def test_cheap_risky_order_is_auto_rejected(self):
        """< $10 and high risk -> bypass LLM, cancel in Shopify."""
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            result = self.Risk.process_webhook(self._payload(7.5, "high"))
        self.assertEqual(result["action"], "auto_reject")
        self.assertTrue(result["shopify_cancelled"])
        mock_cancel.assert_called_once()
        task = self.Task.search([("name", "=", result["task"])])
        self.assertEqual(task.state, "bypassed")
        self.assertEqual(task.risk_level, "high")

    def test_cheap_medium_risk_order_is_auto_rejected(self):
        """< $10 and medium risk also qualifies for the bypass."""
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True):
            result = self.Risk.process_webhook(self._payload(3.0, "medium"))
        self.assertEqual(result["action"], "auto_reject")

    def test_expensive_high_risk_order_is_dispatched(self):
        """>= $10 and high risk -> escalate to the agent, no Shopify cancel."""
        with (
            patch.object(
                type(self.Task), "dispatch_fraud_workflow", return_value={"run_id": "run-123"}
            ) as mock_dispatch,
            patch.object(type(self.Task), "_cancel_in_shopify") as mock_cancel,
        ):
            result = self.Risk.process_webhook(self._payload(149.0, "high"))
        self.assertEqual(result["action"], "dispatched")
        mock_dispatch.assert_called_once()
        mock_cancel.assert_not_called()
        task = self.Task.search([("name", "=", result["task"])])
        self.assertEqual(task.state, "queued")

    def test_cheap_low_risk_order_is_ignored(self):
        """Cheap but only low risk -> recorded, not cancelled, not dispatched."""
        with (
            patch.object(type(self.Task), "_cancel_in_shopify") as mock_cancel,
            patch.object(type(self.Task), "dispatch_fraud_workflow") as mock_dispatch,
        ):
            result = self.Risk.process_webhook(self._payload(2.0, "low"))
        self.assertEqual(result["action"], "ignored")
        mock_cancel.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_auto_reject_disabled_falls_through_to_dispatch(self):
        """With the bypass disabled, even cheap risky orders go to the agent."""
        self.env["ir.config_parameter"].sudo().set_param("odoo_ai_ops.auto_reject_enabled", "False")
        with patch.object(
            type(self.Task), "dispatch_fraud_workflow", return_value={"run_id": "run-9"}
        ) as mock_dispatch:
            result = self.Risk.process_webhook(self._payload(5.0, "high"))
        self.assertEqual(result["action"], "dispatched")
        mock_dispatch.assert_called_once()

    def test_approval_flow_sets_state_and_decision(self):
        """ai_ops_set_approval persists the manager decision."""
        task = self.Task.create(
            {
                "task_type": "fraud",
                "risk_level": "high",
                "shopify_order_id": "999",
                "state": "pending_approval",
            }
        )
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            out = task.ai_ops_set_approval("reject", manager_name="Dana")
        self.assertEqual(out["decision"], "reject")
        self.assertEqual(task.decision, "reject")
        self.assertEqual(task.state, "done")
        # The relayed manager is recorded by name; approver_id is only set here
        # because the test runs as a regular Odoo user, not the agent user.
        self.assertEqual(task.approver_name, "Dana")
        self.assertEqual(task.approver_id, self.env.user)
        mock_cancel.assert_called_once()

    def test_duplicate_webhook_is_idempotent(self):
        """A redelivered order-risk webhook must not create a second task."""
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            first = self.Risk.process_webhook(self._payload(7.5, "high", order_id="77001"))
            second = self.Risk.process_webhook(self._payload(7.5, "high", order_id="77001"))
        self.assertEqual(first["action"], "auto_reject")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(second["task"], first["task"])
        mock_cancel.assert_called_once()
        tasks = self.Task.search([("shopify_order_id", "=", "77001")])
        self.assertEqual(len(tasks), 1)

    def test_failed_task_does_not_block_reprocessing(self):
        """A failed dispatch may be retried when the webhook is redelivered."""
        with patch.object(type(self.Task), "dispatch_fraud_workflow", return_value={"run_id": "r1"}) as mock_dispatch:
            first = self.Risk.process_webhook(self._payload(150.0, "high", order_id="77002"))
            self.Task.search([("name", "=", first["task"])]).write({"state": "failed"})
            second = self.Risk.process_webhook(self._payload(150.0, "high", order_id="77002"))
        self.assertEqual(second["action"], "dispatched")
        self.assertEqual(mock_dispatch.call_count, 2)

    # ------------------------------------------------------------------
    # Correlation to the imported sale.order (risk-assessment webhook has no
    # total, so it must be recovered from the order).
    # ------------------------------------------------------------------
    def _risk_only(self, risk, order_id):
        """A risk-assessment payload with NO total (the real Shopify shape)."""
        return {"order_id": order_id, "risk_level": risk}

    def test_risk_total_recovered_from_sale_order(self):
        """No total in the payload -> read it from the correlated order (cheap)."""
        order = self._make_order(6.0, "RC-1")
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            result = self.Risk.process_webhook(self._risk_only("high", "RC-1"))
        self.assertEqual(result["action"], "auto_reject")
        self.assertEqual(result["order_total"], 6.0)
        self.assertTrue(result["order_cancelled"])
        mock_cancel.assert_called_once()
        self.assertEqual(order.state, "cancel")
        task = self.Task.search([("name", "=", result["task"])])
        self.assertEqual(task.sale_order_id, order)
        self.assertEqual(order.shopify_risk_level, "high")

    def test_expensive_order_from_sale_order_is_dispatched(self):
        """Total recovered from a pricey order -> escalate, do not auto-cancel."""
        self._make_order(200.0, "RC-2")
        with (
            patch.object(type(self.Task), "dispatch_fraud_workflow", return_value={"run_id": "r2"}) as mock_dispatch,
            patch.object(type(self.Task), "_cancel_in_shopify") as mock_cancel,
        ):
            result = self.Risk.process_webhook(self._risk_only("high", "RC-2"))
        self.assertEqual(result["action"], "dispatched")
        self.assertEqual(result["order_total"], 200.0)
        mock_dispatch.assert_called_once()
        mock_cancel.assert_not_called()

    def test_unknown_total_is_not_treated_as_cheap(self):
        """No payload total AND no correlated order -> never auto-cancel; escalate."""
        with (
            patch.object(type(self.Task), "dispatch_fraud_workflow", return_value={"run_id": "r3"}) as mock_dispatch,
            patch.object(type(self.Task), "_cancel_in_shopify") as mock_cancel,
        ):
            result = self.Risk.process_webhook(self._risk_only("high", "RC-NOORDER"))
        self.assertEqual(result["action"], "dispatched")
        mock_dispatch.assert_called_once()
        mock_cancel.assert_not_called()

    def test_benign_then_risky_assessment_escalates(self):
        """A later risky assessment must supersede an earlier low/no-risk one."""
        self._make_order(4.0, "RC-3")
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            first = self.Risk.process_webhook(self._risk_only("low", "RC-3"))
            second = self.Risk.process_webhook(self._risk_only("high", "RC-3"))
        self.assertEqual(first["action"], "ignored")
        self.assertEqual(second["action"], "auto_reject")
        mock_cancel.assert_called_once()

    def test_repeated_benign_assessment_is_deduped(self):
        """Two low assessments should not create two log tasks."""
        self._make_order(50.0, "RC-4")
        first = self.Risk.process_webhook(self._risk_only("low", "RC-4"))
        second = self.Risk.process_webhook(self._risk_only("none", "RC-4"))
        self.assertEqual(first["action"], "ignored")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(len(self.Task.search([("shopify_order_id", "=", "RC-4")])), 1)

    def test_manager_reject_cancels_sale_order(self):
        """A manager rejection cancels both Shopify and the Odoo order."""
        order = self._make_order(120.0, "RC-5")
        task = self.Task.create(
            {
                "task_type": "fraud",
                "risk_level": "high",
                "shopify_order_id": "RC-5",
                "sale_order_id": order.id,
                "state": "pending_approval",
            }
        )
        with patch.object(type(self.Task), "_cancel_in_shopify", return_value=True) as mock_cancel:
            out = task.ai_ops_set_approval("reject", manager_name="Dana")
        self.assertTrue(out["order_cancelled"])
        self.assertEqual(order.state, "cancel")
        mock_cancel.assert_called_once()
