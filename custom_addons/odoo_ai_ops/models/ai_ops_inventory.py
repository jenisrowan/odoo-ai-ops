# -*- coding: utf-8 -*-
"""Reconciliation & inventory actions exposed to the agent over JSON-RPC.

These are plain public model methods, so the FastAPI agent reaches them with a
standard Odoo ``call_kw`` JSON-RPC call, e.g.::

    call_kw("ai.ops.inventory", "query_catalog", [[("type", "=", "consu")]], {"limit": 50})

They cover the three operations the inventory-reconciliation workflow needs:
look up catalog records, inspect historical warehouse moves, and write an
inventory adjustment patch back into Odoo.

Implemented on an ``AbstractModel`` (no own table). Access is still gated by the
calling user's record rules / ACLs - the agent authenticates as a dedicated
integration user that belongs to the *AI Ops Agent* group.
"""

import logging

from odoo import api, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Safety cap so a runaway agent query cannot try to read the whole database.
MAX_LIMIT = 500


class AiOpsInventory(models.AbstractModel):
    _name = "ai.ops.inventory"
    _description = "AI Ops Inventory & Reconciliation API"

    @api.model
    def _clamp_limit(self, limit):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        return max(1, min(limit, MAX_LIMIT))

    # ------------------------------------------------------------------
    # 1. Query catalog records
    # ------------------------------------------------------------------
    @api.model
    def query_catalog(self, domain=None, fields=None, limit=100):
        """Return catalog (``product.product``) records matching ``domain``.

        :param domain: Odoo search domain (list of tuples). Defaults to all
                       storable products.
        :param fields: which fields to read. A safe default set is used when
                       omitted.
        """
        domain = domain or [("is_storable", "=", True)]
        fields = fields or [
            "id",
            "default_code",
            "barcode",
            "name",
            "qty_available",
            "virtual_available",
            "list_price",
            "standard_price",
        ]
        products = self.env["product.product"].search(domain, limit=self._clamp_limit(limit))
        return products.read(fields)

    # ------------------------------------------------------------------
    # 2. Historical warehouse moves
    # ------------------------------------------------------------------
    @api.model
    def warehouse_moves(self, product_id, limit=100, states=None):
        """Return historical stock moves for a product, newest first.

        :param states: optional list of ``stock.move`` states to filter on
                       (defaults to done moves, i.e. real warehouse history).
        """
        if not product_id:
            raise UserError("product_id is required for warehouse_moves().")
        states = states or ["done"]
        moves = self.env["stock.move"].search(
            [("product_id", "=", int(product_id)), ("state", "in", states)],
            limit=self._clamp_limit(limit),
            order="date desc",
        )
        return moves.read(
            [
                "id",
                "reference",
                "date",
                "product_uom_qty",
                "quantity",
                "location_id",
                "location_dest_id",
                "state",
                "origin",
            ]
        )

    # ------------------------------------------------------------------
    # 3. Write an inventory adjustment patch
    # ------------------------------------------------------------------
    @api.model
    def apply_inventory_patch(self, product_id, counted_qty, location_id=None, reason=None):
        """Apply an inventory adjustment so on-hand quantity == ``counted_qty``.

        Uses the Odoo 19 ``stock.quant`` inventory-adjustment flow: set
        ``inventory_quantity`` on the (product, location) quant and apply it.
        Returns a summary dict with the resulting on-hand quantity.

        This is the agent's write-back path; it should only be reached *after*
        a human approved the reconciliation, but we still validate inputs
        defensively.
        """
        if not product_id:
            raise UserError("product_id is required for apply_inventory_patch().")
        try:
            counted_qty = float(counted_qty)
        except (TypeError, ValueError) as exc:
            raise ValidationError("counted_qty must be a number.") from exc

        product = self.env["product.product"].browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)

        if location_id:
            location = self.env["stock.location"].browse(int(location_id))
        else:
            # Default to the company's main internal stock location.
            warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], limit=1)
            location = warehouse.lot_stock_id
        if not location:
            raise UserError("Could not resolve a stock location for the adjustment.")

        Quant = self.env["stock.quant"].with_context(inventory_mode=True)
        quant = Quant.search(
            [
                ("product_id", "=", product.id),
                ("location_id", "=", location.id),
            ],
            limit=1,
        )
        if quant:
            quant.inventory_quantity = counted_qty
        else:
            quant = Quant.create(
                {
                    "product_id": product.id,
                    "location_id": location.id,
                    "inventory_quantity": counted_qty,
                }
            )
        quant.action_apply_inventory()

        _logger.info(
            "AI Ops: applied inventory patch for product %s at %s -> %.3f (%s)",
            product.display_name,
            location.display_name,
            counted_qty,
            reason or "no reason",
        )
        return {
            "product_id": product.id,
            "location_id": location.id,
            "counted_qty": counted_qty,
            "on_hand_after": product.with_context(location=location.id).qty_available,
            "reason": reason,
        }
