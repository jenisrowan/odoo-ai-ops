# -*- coding: utf-8 -*-
"""Shopify ``orders/create`` -> Odoo ``sale.order`` intake.

This is the ingestion half of the two-event design:

* ``orders/create`` (this model) builds a **confirmed** ``sale.order`` the moment
  an order is placed, storing the full raw payload on it.
* ``orders/risk_assessment_changed`` (``ai.ops.order.risk``) arrives later - once
  Shopify's asynchronous fraud analysis completes - and cancels that order if it
  is risky.

Design notes
------------
* **Never lose an order.** Unmapped SKUs auto-create a minimal product and the
  whole payload is persisted verbatim, so a catalog gap can never drop a sale.
  Any hard failure is raised so the caller returns 5xx and SQS redelivers.
* **Idempotent.** ``orders/create`` can be redelivered (SQS is at-least-once and
  Shopify retries); a second delivery for the same ``shopify_order_id`` returns
  the existing order instead of duplicating it.
* **Prices come from Shopify.** Each line's ``price_unit`` is taken straight from
  the Shopify line price and Odoo taxes are cleared, so the Odoo order total
  matches Shopify without double-taxing (we do not invoice here). The real tax
  breakdown lives in ``shopify_raw_payload``.

Implemented as an ``AbstractModel`` service (no table of its own), mirroring
``ai.ops.order.risk``.
"""

import json
import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class AiOpsOrderIntake(models.AbstractModel):
    _name = "ai.ops.order.intake"
    _description = "AI Ops Shopify Order Intake"

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------
    @api.model
    def _extract_order_identity(self, order):
        order_id = order.get("id") or order.get("order_id") or order.get("admin_graphql_api_id")
        name = order.get("name")
        if not name and order.get("order_number"):
            name = "#%s" % order["order_number"]
        return (str(order_id) if order_id is not None else False, name and str(name) or False)

    @api.model
    def _find_or_create_partner(self, order):
        """Resolve the ordering customer to a ``res.partner`` (create if new).

        Matched by email (the only stable identifier we can rely on). Address is
        best-effort from the shipping/billing block; the full address always
        remains in the raw payload.
        """
        Partner = self.env["res.partner"]
        customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
        email = order.get("email") or customer.get("email") or order.get("contact_email")

        addr = order.get("shipping_address") or order.get("billing_address") or {}
        if not isinstance(addr, dict):
            addr = {}

        first = customer.get("first_name") or addr.get("first_name") or ""
        last = customer.get("last_name") or addr.get("last_name") or ""
        name = (first + " " + last).strip() or addr.get("name") or email or _("Shopify Customer")

        if email:
            partner = Partner.search([("email", "=ilike", email)], limit=1)
            if partner:
                return partner

        country = self.env["res.country"]
        code = addr.get("country_code") or addr.get("country")
        if code:
            country = country.search([("code", "=", str(code).upper())], limit=1)

        return Partner.create(
            {
                "name": name,
                "email": email or False,
                "phone": order.get("phone") or customer.get("phone") or addr.get("phone") or False,
                "street": addr.get("address1") or False,
                "street2": addr.get("address2") or False,
                "city": addr.get("city") or False,
                "zip": addr.get("zip") or False,
                "country_id": country.id or False,
            }
        )

    @api.model
    def _find_or_create_product(self, item):
        """Resolve a Shopify line item to a ``product.product`` (create if new).

        Matched by SKU (``default_code``). Unknown SKUs auto-create a minimal
        goods product so an order is never rejected over a catalog gap - keeping
        the catalog tidy afterwards is the inventory team's concern, not ours.
        """
        Product = self.env["product.product"]
        sku = (item.get("sku") or "").strip()
        title = item.get("title") or item.get("name") or _("Shopify Item")

        if sku:
            product = Product.search([("default_code", "=", sku)], limit=1)
            if product:
                return product

        try:
            price = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0

        return Product.create(
            {
                "name": title,
                "default_code": sku or False,
                "type": "consu",
                "list_price": price,
                "sale_ok": True,
            }
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @api.model
    def process_order_create(self, payload):
        """Build (and confirm) a ``sale.order`` from a Shopify order payload.

        Returns a JSON-serialisable dict for the agent/caller.
        """
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}

        order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
        order_id, order_name = self._extract_order_identity(order)

        SaleOrder = self.env["sale.order"]

        # Idempotency: a redelivered orders/create must not create a 2nd order.
        if order_id:
            existing = SaleOrder.search([("shopify_order_id", "=", order_id)], limit=1)
            if existing:
                _logger.info(
                    "AI Ops: duplicate orders/create for Shopify order %s (sale.order %s) - skipping.",
                    order_id,
                    existing.name,
                )
                return {
                    "action": "duplicate",
                    "sale_order": existing.name,
                    "sale_order_id": existing.id,
                    "state": existing.state,
                }

        partner = self._find_or_create_partner(order)

        so = SaleOrder.create(
            {
                "partner_id": partner.id,
                "origin": order_name or (order_id and "Shopify %s" % order_id) or "Shopify",
                "shopify_order_id": order_id,
                "shopify_order_name": order_name,
                "shopify_raw_payload": json.dumps(payload),
            }
        )

        line_items = order.get("line_items") or []
        if isinstance(line_items, dict):  # tolerate a single-object shape
            line_items = [line_items]
        for item in line_items:
            if not isinstance(item, dict):
                continue
            product = self._find_or_create_product(item)
            try:
                qty = float(item.get("quantity") or 1.0)
            except (TypeError, ValueError):
                qty = 1.0
            try:
                price = float(item.get("price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            line = self.env["sale.order.line"].create(
                {
                    "order_id": so.id,
                    "product_id": product.id,
                    "product_uom_qty": qty,
                }
            )
            # Override price/tax *after* creation: setting product_id in create
            # triggers the price/tax computes, which would otherwise clobber the
            # Shopify price. Clearing tax_ids keeps the Odoo total == Shopify total.
            line.write({"price_unit": price, "tax_ids": [(5, 0, 0)]})

        # Bring it in as a confirmed order (the customer already paid). Best-effort:
        # if confirmation is refused we keep the captured draft rather than lose it.
        confirmed = False
        try:
            so.action_confirm()
            confirmed = so.state in ("sale", "done")
        except Exception as exc:  # noqa: BLE001 - capture beats losing the order
            _logger.exception("AI Ops: could not confirm Shopify order %s; left as draft.", order_id)
            so.message_post(body=_("Order captured but not confirmed automatically: %s", exc))

        _logger.info("AI Ops: imported Shopify order %s as %s (state=%s).", order_id, so.name, so.state)
        return {
            "action": "created",
            "sale_order": so.name,
            "sale_order_id": so.id,
            "confirmed": confirmed,
            "state": so.state,
        }
