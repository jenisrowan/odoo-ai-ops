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
        mock_cancel.assert_called_once()
