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

from odoo import api, fields, models
from odoo.addons.odoo_ai_ops.services.shopify_client import ShopifyClient
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# stock.move states that have NOT decremented/incremented on-hand yet.
OPEN_MOVE_STATES = ["draft", "waiting", "confirmed", "assigned", "partially_available"]

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

    # ------------------------------------------------------------------
    # 4. Discrepancy context (root-cause analysis input)
    # ------------------------------------------------------------------
    def _shopify_client(self):
        settings = self.env["res.config.settings"]
        return ShopifyClient(
            shop_domain=settings._ai_ops_get_param("odoo_ai_ops.shopify_shop_domain"),
            admin_token=settings._ai_ops_get_param("odoo_ai_ops.shopify_admin_token"),
            api_version=settings._ai_ops_get_param("odoo_ai_ops.shopify_api_version", "2025-01"),
        )

    @api.model
    def discrepancy_context(self, product_id, fetch_shopify=True, stale_days=3):
        """Gather everything the agent needs to explain a stock discrepancy.

        Returns Odoo on-hand vs. Shopify available, plus the evidence that
        usually explains a divergence: outgoing/incoming moves still open (a
        shipment shipped but not validated, or a receipt not recorded), and
        recent sales orders with their delivery status (a missing/undelivered
        order). The agent reasons over this to determine the root cause.
        """
        if not product_id:
            raise UserError("product_id is required for discrepancy_context().")
        product = self.env["product.product"].browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)

        Move = self.env["stock.move"]
        stale_cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=int(stale_days))

        def _serialize(moves):
            rows = []
            for m in moves:
                rows.append(
                    {
                        "id": m.id,
                        "reference": m.reference,
                        "date": str(m.date) if m.date else None,
                        "demand_qty": m.product_uom_qty,
                        "done_qty": m.quantity,
                        "state": m.state,
                        "origin": m.origin,
                        "picking": m.picking_id.name or None,
                        # An open move older than the threshold is a prime suspect
                        # (e.g. a shipment already shipped but never validated).
                        "aged": bool(m.date and m.date < stale_cutoff),
                    }
                )
            return rows

        pending_out = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "in", OPEN_MOVE_STATES),
                ("location_dest_id.usage", "=", "customer"),
            ],
            order="date",
            limit=50,
        )
        pending_in = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "in", OPEN_MOVE_STATES),
                ("location_id.usage", "=", "supplier"),
            ],
            order="date",
            limit=50,
        )

        # Shopify available quantity, matched by SKU (default_code) or barcode.
        sku = product.default_code or product.barcode
        shopify_qty = None
        shopify_error = None
        if fetch_shopify and sku:
            try:
                shopify_qty = self._shopify_client().get_available_inventory(sku)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully for the agent
                shopify_error = str(exc)
                _logger.warning("AI Ops: Shopify inventory lookup failed for %s: %s", sku, exc)

        on_hand = product.qty_available
        discrepancy = (on_hand - shopify_qty) if shopify_qty is not None else None

        # Recent sales orders (only if the 'sale' app is installed).
        recent_sale_orders = []
        if "sale.order.line" in self.env:
            lines = self.env["sale.order.line"].search(
                [("product_id", "=", product.id)], order="create_date desc", limit=20
            )
            for line in lines:
                order = line.order_id
                recent_sale_orders.append(
                    {
                        "order": order.name,
                        "state": order.state,
                        "date_order": str(order.date_order) if order.date_order else None,
                        "qty_ordered": line.product_uom_qty,
                        "qty_delivered": line.qty_delivered,
                        "undelivered": line.product_uom_qty - line.qty_delivered,
                    }
                )

        return {
            "product": {"id": product.id, "sku": sku, "name": product.display_name},
            "odoo_on_hand": on_hand,
            "odoo_free_qty": product.free_qty,
            "odoo_incoming": product.incoming_qty,
            "odoo_outgoing": product.outgoing_qty,
            "shopify_available": shopify_qty,
            "shopify_error": shopify_error,
            "discrepancy_odoo_minus_shopify": discrepancy,
            "pending_outgoing_moves": _serialize(pending_out),
            "pending_incoming_moves": _serialize(pending_in),
            "recent_sale_orders": recent_sale_orders,
            "stale_days": int(stale_days),
        }

    # ------------------------------------------------------------------
    # 5. Push Odoo's on-hand back to Shopify (Odoo is source of truth)
    # ------------------------------------------------------------------
    @api.model
    def push_inventory_to_shopify(self, product_id, qty, reason=None):
        """Set Shopify's available quantity for a product to ``qty``.

        Used for the "Odoo has more stock — Shopify undercount / human error"
        resolution: Odoo is authoritative, so we correct Shopify.
        """
        if not product_id:
            raise UserError("product_id is required for push_inventory_to_shopify().")
        product = self.env["product.product"].browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)
        sku = product.default_code or product.barcode
        if not sku:
            raise UserError("Product %s has no SKU/barcode to match in Shopify." % product.display_name)
        result = self._shopify_client().set_inventory_quantity(sku, float(qty), reason="correction")
        _logger.info(
            "AI Ops: pushed inventory for %s (sku=%s) -> %.3f in Shopify (%s)",
            product.display_name,
            sku,
            float(qty),
            reason or "reconciliation",
        )
        return {"product_id": product.id, "sku": sku, "shopify_qty": float(qty), "result": result}
