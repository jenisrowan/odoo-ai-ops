# -*- coding: utf-8 -*-
"""``ai.ops.task`` - the persistent record of an AI operation.

One record is created per AI workflow run (fraud validation or inventory
reconciliation). It is the single source of truth for the human-in-the-loop
state machine: the agent reports progress and the final manager decision back
onto this record over JSON-RPC, and Odoo executes the resulting side effects
(e.g. cancelling the order in Shopify).

State machine
-------------
    draft ─► queued ─► running ─► pending_approval ─► approved ─► done
                          │                        └► rejected ─► done
                          └► failed
    (cheap-order rule)  ─► bypassed
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.addons.odoo_ai_ops.services.agent_client import AgentClient, AgentError
from odoo.addons.odoo_ai_ops.services.shopify_client import ShopifyClient, ShopifyError
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AiOpsTask(models.Model):
    _name = "ai.ops.task"
    _description = "AI Ops Task"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        index=True,
        default=lambda self: _("New"),
    )
    task_type = fields.Selection(
        [
            ("fraud", "Fraud Validation"),
            ("reconciliation", "Inventory Reconciliation"),
        ],
        required=True,
        default="fraud",
        tracking=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("pending_approval", "Pending Approval"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("bypassed", "Bypassed (Auto-Rejected)"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="draft",
        required=True,
        tracking=True,
        index=True,
    )
    risk_level = fields.Selection(
        [
            ("none", "None"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
        ],
        default="none",
        tracking=True,
    )

    # --- Shopify order context (fraud workflow) ---
    shopify_order_id = fields.Char(string="Shopify Order ID", index=True, copy=False)
    shopify_order_name = fields.Char(string="Shopify Order Name")
    order_total = fields.Monetary(string="Order Total", currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)

    # --- Product context (reconciliation workflow) ---
    product_id = fields.Many2one("product.product", string="Product")

    # --- Agent / workflow linkage ---
    agent_run_id = fields.Char(
        string="Agent Run ID",
        copy=False,
        help="LangGraph thread/run identifier reported by the agent cluster.",
    )
    payload = fields.Text(string="Raw Payload", copy=False)
    analysis = fields.Text(string="AI Analysis", tracking=True)
    bypass_reason = fields.Char(string="Bypass Reason")

    # --- Final decision ---
    decision = fields.Selection(
        [("approve", "Approve"), ("reject", "Reject & Cancel")],
        string="Final Decision",
        tracking=True,
        copy=False,
    )
    approver_id = fields.Many2one(
        "res.users",
        string="Approved/Rejected By",
        copy=False,
        help="Only set when the decision was made by an Odoo user directly. "
        "Decisions relayed from Slack by the agent record the manager in "
        "'Approver Name' instead (the agent's technical user is not the approver).",
    )
    approver_name = fields.Char(
        string="Approver Name",
        copy=False,
        tracking=True,
        help="Display name of the manager who decided (e.g. the Slack user), as relayed by the agent.",
    )
    approval_date = fields.Datetime(string="Decision Date", copy=False)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)

    # ------------------------------------------------------------------
    # ORM overrides
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("ai.ops.task") or _("New")
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def _settings(self):
        """Return the config helper bound to the environment."""
        return self.env["res.config.settings"]

    def _get_param(self, key, default=None):
        return self._settings()._ai_ops_get_param(key, default=default)

    def _agent_client(self):
        return AgentClient(
            base_url=self._get_param("odoo_ai_ops.agent_base_url"),
            token=self._get_param("odoo_ai_ops.shared_token"),
        )

    def _shopify_client(self):
        return ShopifyClient(
            shop_domain=self._get_param("odoo_ai_ops.shopify_shop_domain"),
            admin_token=self._get_param("odoo_ai_ops.shopify_admin_token"),
            api_version=self._get_param("odoo_ai_ops.shopify_api_version", "2026-07"),
        )

    # ------------------------------------------------------------------
    # Agent dispatch (Odoo -> FastAPI)
    # ------------------------------------------------------------------
    def dispatch_fraud_workflow(self):
        """Hand the fraud-validation workflow off to the agent cluster."""
        self.ensure_one()
        order = {}
        if self.payload:
            try:
                order = json.loads(self.payload)
            except (ValueError, TypeError):
                order = {}
        try:
            result = self._agent_client().start_fraud_workflow(
                task_ref=self.name,
                order=order,
                risk_level=self.risk_level or "high",
                task_id=self.id,
            )
        except AgentError as exc:
            self.write({"state": "failed"})
            self.message_post(body=_("Failed to dispatch to agent: %s", exc))
            _logger.exception("AI Ops: agent dispatch failed for %s", self.name)
            raise UserError(_("Could not reach the AI agent cluster: %s", exc)) from exc

        self.write(
            {
                "state": "running",
                "agent_run_id": result.get("run_id") or self.agent_run_id,
            }
        )
        self.message_post(body=_("Fraud workflow dispatched to agent (run %s).", result.get("run_id") or "n/a"))
        return result

    def dispatch_reconciliation_workflow(self):
        """Hand the inventory-reconciliation workflow off to the agent cluster.

        Odoo-initiated entry point for Path 1: an administrator flags a stock
        mismatch for a product; the agent gathers context (catalog + moves via
        JSON-RPC), proposes a correction, and requests Slack approval.
        """
        self.ensure_one()
        if not self.product_id:
            raise UserError(_("A product is required to start a reconciliation task."))
        context = {}
        if self.payload:
            try:
                context = json.loads(self.payload)
            except (ValueError, TypeError):
                context = {}
        try:
            result = self._agent_client().start_reconciliation_workflow(
                task_ref=self.name,
                product_id=self.product_id.id,
                context=context,
                task_id=self.id,
            )
        except AgentError as exc:
            self.write({"state": "failed"})
            self.message_post(body=_("Failed to dispatch to agent: %s", exc))
            _logger.exception("AI Ops: reconciliation dispatch failed for %s", self.name)
            raise UserError(_("Could not reach the AI agent cluster: %s", exc)) from exc

        self.write({"state": "running", "agent_run_id": result.get("run_id") or self.agent_run_id})
        self.message_post(
            body=_("Reconciliation workflow dispatched to agent (run %s).", result.get("run_id") or "n/a")
        )
        return result

    @api.model
    def start_reconciliation_for_product(self, product_id, context=None):
        """Create a reconciliation task for a product and dispatch it.

        Convenience entry point used by the ``product.product`` action so an
        administrator can trigger Path 1 directly from a product record.
        """
        product = self.env["product.product"].browse(int(product_id))
        if not product.exists():
            raise UserError(_("Unknown product."))
        task = self.create(
            {
                "task_type": "reconciliation",
                "product_id": product.id,
                "state": "queued",
                "payload": json.dumps(context or {}),
            }
        )
        task.dispatch_reconciliation_workflow()
        return task

    # ------------------------------------------------------------------
    # Agent callbacks (FastAPI -> Odoo, via JSON-RPC)
    # ------------------------------------------------------------------
    def register_agent_run(self, run_id=None, state="pending_approval", analysis=None):
        """Called by the agent to attach its run id and report progress.

        Typically invoked when the agent has produced its analysis and is now
        blocked on a human (Slack) decision -> moves us to ``pending_approval``.
        """
        self.ensure_one()
        vals = {}
        if run_id:
            vals["agent_run_id"] = run_id
        if state in dict(self._fields["state"].selection):
            vals["state"] = state
        if analysis is not None:
            vals["analysis"] = analysis
        self.write(vals)
        self.message_post(body=_("Agent update - state: %s", vals.get("state", self.state)))
        return True

    def ai_ops_set_approval(self, decision, manager_name=None, note=None, run_id=None):
        """Persist a manager's decision relayed by the agent from Slack.

        :param decision: ``'approve'`` or ``'reject'``.
        Returns a small dict the agent can log. When the decision is a
        rejection, Odoo executes the cancellation in Shopify (per the
        architecture's "Odoo executes the final decision" step).
        """
        self.ensure_one()
        if decision not in ("approve", "reject"):
            raise UserError(_("Invalid decision '%s'.", decision))

        vals = {
            "decision": decision,
            "approval_date": fields.Datetime.now(),
            "approver_name": manager_name or self.env.user.name,
            "state": "approved" if decision == "approve" else "rejected",
        }
        # Attribute approver_id only to real Odoo users. When the call arrives
        # over JSON-RPC from the agent's technical user, the decision maker is
        # the (Slack) manager in ``approver_name``, not the integration account.
        if not self.env.user.has_group("odoo_ai_ops.group_ai_ops_agent"):
            vals["approver_id"] = self.env.user.id
        if run_id:
            vals["agent_run_id"] = run_id
        self.write(vals)

        body = _("Manager decision relayed from Slack: %s", decision.upper())
        if manager_name:
            body += _(" (by %s)", manager_name)
        if note:
            body += "\n%s" % note
        self.message_post(body=body)

        cancelled = False
        if decision == "reject" and self.task_type == "fraud" and self.shopify_order_id:
            cancelled = self._cancel_in_shopify(reason="FRAUD", staff_note="AI fraud review: rejected")
        # The workflow is now resolved.
        self.write({"state": "done"})
        return {"task": self.name, "decision": decision, "shopify_cancelled": cancelled}

    # ------------------------------------------------------------------
    # Manual (Odoo-side) overrides
    # ------------------------------------------------------------------
    def action_approve(self):
        for task in self:
            task.ai_ops_set_approval("approve", manager_name=self.env.user.name, note=_("Approved in Odoo"))
        return True

    def action_reject(self):
        for task in self:
            task.ai_ops_set_approval("reject", manager_name=self.env.user.name, note=_("Rejected in Odoo"))
        return True

    # ------------------------------------------------------------------
    # Shopify side effects
    # ------------------------------------------------------------------
    def _cancel_in_shopify(self, reason="FRAUD", staff_note=None):
        """Cancel this task's Shopify order. Returns True on success."""
        self.ensure_one()
        if not self.shopify_order_id:
            return False
        # Refunding on cancellation is an explicit opt-in: auto-refunding a
        # fraud rejection is usually wrong (void/review the payment instead).
        refund = str(self._get_param("odoo_ai_ops.refund_on_cancel", "False")).strip().lower() in (
            "true",
            "1",
        )
        try:
            self._shopify_client().cancel_order(
                self.shopify_order_id, reason=reason, refund=refund, staff_note=staff_note
            )
        except ShopifyError as exc:
            _logger.exception("AI Ops: Shopify cancel failed for %s", self.name)
            self.message_post(body=_("Shopify cancellation FAILED: %s", exc))
            return False
        self.message_post(
            body=_(
                "Order %s cancelled in Shopify (reason: %s).", self.shopify_order_name or self.shopify_order_id, reason
            )
        )
        return True

    # ------------------------------------------------------------------
    # Scheduled housekeeping
    # ------------------------------------------------------------------
    @api.model
    def _cron_expire_stale_tasks(self, max_hours=72):
        """Fail tasks stuck waiting on a human/agent for too long.

        Run from ``ir.cron``. A task left in ``running`` or ``pending_approval``
        past ``max_hours`` almost certainly lost its Slack callback (e.g. the
        Valkey checkpoint expired); we close it out as ``failed`` so it stops
        appearing as actionable and surfaces in reporting.
        """
        deadline = fields.Datetime.subtract(fields.Datetime.now(), hours=max_hours)
        stale = self.search(
            [
                ("state", "in", ("running", "pending_approval")),
                ("write_date", "<", deadline),
            ]
        )
        for task in stale:
            task.message_post(body=_("Task auto-expired after %s h with no resolution.", max_hours))
        stale.write({"state": "failed"})
        if stale:
            _logger.info("AI Ops: expired %s stale task(s).", len(stale))
        return True
