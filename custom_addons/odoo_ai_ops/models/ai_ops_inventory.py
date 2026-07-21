# -*- coding: utf-8 -*-
"""Reconciliation & inventory actions exposed to the agent over JSON-RPC.

These are plain public model methods, so the FastAPI agent reaches them with a
standard Odoo ``call_kw`` JSON-RPC call, e.g.::

    call_kw("ai.ops.inventory", "query_catalog", [[("type", "=", "consu")]], {"limit": 50})

Most are read-only evidence gathering, which is what the agent's investigation
loop calls as tools: look up catalog records, inspect historical warehouse
moves, drill into specific suspect moves, and list sale order lines. Two write
methods - ``apply_inventory_patch`` and ``push_inventory_to_shopify`` - are not
tools and are never called by the model; the graph invokes them directly once a
manager has approved.

Implemented on an ``AbstractModel`` (no own table). This module is the agent's
entire Odoo surface by design: the agent's user holds no stock manager role and
is denied write/create/unlink on the stock models by global record rules, so
these methods (which elevate internally, after the approval gate) are the only
way its credential can affect stock. See :meth:`AiOpsInventory._access`.
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

# Fields the catalog reader may return. The agent's reads are elevated (see
# _access), so the readable surface is pinned here rather than left to the
# caller - otherwise "read-only" would still mean "read every field".
CATALOG_FIELDS = (
    "id",
    "default_code",
    "barcode",
    "name",
    "qty_available",
    "virtual_available",
    "free_qty",
    "incoming_qty",
    "outgoing_qty",
    "list_price",
    "standard_price",
    "is_storable",
    "active",
)


def _move_kind(move):
    """Classify a stock.move by what actually happened to the stock.

    The investigation reasons about causes ("someone forced the count", "it was
    moved to another warehouse"), not about location ids. Odoo encodes the two
    special cases as flags rather than location usage: ``is_inventory`` marks a
    move created by an inventory adjustment, and ``scrap_id`` marks a scrap.
    Everything else falls out of the source/destination usage pair.
    """
    if move.is_inventory:
        return "inventory_adjustment"
    if move.scrap_id:
        return "scrap"
    src = move.location_id.usage
    dest = move.location_dest_id.usage
    if src == "internal" and dest == "internal":
        return "internal_transfer"
    if dest == "internal":
        return "incoming"
    if src == "internal":
        return "outgoing"
    return "other"


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

    @api.model
    def _is_agent_user(self):
        return self.env.user.has_group("odoo_ai_ops.group_ai_ops_agent")

    @api.model
    def _access(self, model_name):
        """Return the recordset used for stock/catalog access.

        The agent's Odoo credential is deliberately powerless on the stock
        models: it holds no manager role, and global record rules deny it
        write/create/unlink on quants, moves, move lines, pickings and lots
        (see ``security/ai_ops_security.xml``). Its effective power is exactly
        the methods on this model, so a leaked JSON-RPC password cannot move
        stock through a generic ``execute_kw``.

        These methods therefore elevate for the agent - on the write paths only
        ever *after* :meth:`_require_approved_task` has run. Human callers keep
        their own rights, so a plain AI Ops user gains nothing by calling here
        that they could not already do in the UI.
        """
        records = self.env[model_name]
        return records.sudo() if self._is_agent_user() else records

    @api.model
    def _company_domain(self, field="company_id", allow_shared=True):
        """Multi-company scoping for the elevated searches.

        ``sudo()`` bypasses record rules, including the multi-company rule the
        agent would otherwise inherit, so the company filter has to be explicit
        or the agent would read across companies.
        """
        companies = self.env.companies.ids
        if allow_shared:
            return ["|", (field, "=", False), (field, "in", companies)]
        return [(field, "in", companies)]

    @api.model
    def _serialize_moves(self, moves, stale_cutoff=None):
        """One move shape for every reader, so the model learns it once.

        ``user`` is the move's author: for an inventory adjustment that is the
        person who forced the count, which is usually the whole answer.
        """
        rows = []
        for move in moves:
            row = {
                "id": move.id,
                "reference": move.reference,
                "kind": _move_kind(move),
                "date": str(move.date) if move.date else None,
                "demand_qty": move.product_uom_qty,
                "done_qty": move.quantity,
                "state": move.state,
                "origin": move.origin,
                "location_from": move.location_id.display_name,
                "location_to": move.location_dest_id.display_name,
                "picking": move.picking_id.name or None,
                "user": move.create_uid.display_name or None,
            }
            if stale_cutoff is not None:
                # An open move older than the threshold is a prime suspect
                # (e.g. a shipment already shipped but never validated).
                row["aged"] = bool(move.date and move.date < stale_cutoff)
            if move.is_inventory and move.inventory_name:
                row["adjustment_reason"] = move.inventory_name
            rows.append(row)
        return rows

    @api.model
    def _require_approved_task(self, task_id, product_id):
        """Server-side human-approval gate for the agent's write paths.

        The LangGraph workflow only reaches the write nodes after a manager
        approved in Slack, but that check lives in the agent. If the agent's
        JSON-RPC credential leaked, the write methods would otherwise be
        callable directly. So when the caller is the technical agent user,
        require a matching ``ai.ops.task`` that carries a persisted *approve*
        decision (the agent records the decision via ``ai_ops_set_approval``
        before applying it). Human users (managers) are not gated.
        """
        if not self.env.user.has_group("odoo_ai_ops.group_ai_ops_agent"):
            return
        if not task_id:
            raise UserError("task_id is required: agent inventory writes must reference the approved ai.ops.task.")
        task = self.env["ai.ops.task"].browse(int(task_id))
        if not task.exists():
            raise UserError("Unknown ai.ops.task id %s." % task_id)
        if task.task_type != "reconciliation":
            raise UserError("Task %s is not a reconciliation task." % task.name)
        if task.decision != "approve":
            raise UserError("Task %s has no approved decision recorded; refusing the inventory write." % task.name)
        if task.product_id and task.product_id.id != int(product_id):
            raise UserError(
                "Task %s was approved for product %s, not product %s." % (task.name, task.product_id.id, product_id)
            )

    # ------------------------------------------------------------------
    # 1. Query catalog records
    # ------------------------------------------------------------------
    @api.model
    def query_catalog(self, domain=None, fields=None, limit=100):
        """Return catalog (``product.product``) records matching ``domain``.

        :param domain: Odoo search domain (list of tuples). Defaults to all
                       storable products.
        :param fields: which fields to read. Restricted to
                       :data:`CATALOG_FIELDS`; unknown names are dropped rather
                       than raising, so a model asking for a plausible-sounding
                       field still gets a useful answer.
        """
        domain = list(domain or [("is_storable", "=", True)])
        # The elevated search would otherwise ignore the multi-company rule.
        domain += self._company_domain()
        fields = [f for f in (fields or CATALOG_FIELDS) if f in CATALOG_FIELDS] or list(CATALOG_FIELDS)
        products = self._access("product.product").search(domain, limit=self._clamp_limit(limit))
        return products.read(fields)

    # ------------------------------------------------------------------
    # 2. Historical warehouse moves
    # ------------------------------------------------------------------
    @api.model
    def warehouse_moves(self, product_id, limit=100, states=None, kinds=None):
        """Return historical stock moves for a product, newest first.

        :param states: optional list of ``stock.move`` states to filter on
                       (defaults to done moves, i.e. real warehouse history).
        :param kinds: optional list of :func:`_move_kind` values to keep
                      (``incoming``, ``outgoing``, ``internal_transfer``,
                      ``inventory_adjustment``, ``scrap``, ``other``).
        """
        if not product_id:
            raise UserError("product_id is required for warehouse_moves().")
        states = states or ["done"]
        domain = [("product_id", "=", int(product_id)), ("state", "in", states)]
        if kinds:
            # is_inventory / scrap_id are flags, not usages, so filter on them
            # directly; the rest are narrowed after serialization.
            if kinds == ["inventory_adjustment"]:
                domain.append(("is_inventory", "=", True))
            elif kinds == ["scrap"]:
                domain.append(("scrap_id", "!=", False))
        moves = self._access("stock.move").search(
            domain + self._company_domain(),
            limit=self._clamp_limit(limit),
            order="date desc",
        )
        rows = self._serialize_moves(moves)
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        return rows

    @api.model
    def ledger_check(self, product_id):
        """Consistency canary: do the done moves add up to the quants?

        ``stock.move`` / ``stock.move.line`` is Odoo's ledger; ``stock.quant``
        is the running balance. Everything a user can do is journalled - even
        editing on-hand by hand, which the UI performs by writing the *counted*
        quantity (``inventory_quantity``) and which Odoo records as a move
        tagged ``is_inventory`` against the Inventory-loss location. The
        ``quantity`` field itself is ``readonly``, so the two cannot normally
        drift apart.

        This should therefore essentially never fire. It is here as a canary,
        not as an expected root cause: the only way to break the invariant is
        code that writes the readonly ``quantity`` field through the ORM, which
        is a bug in whatever did it rather than anything an operator can do.
        Treat a gap as "the data is inconsistent, escalate", not as a normal
        explanation for a Shopify divergence.

        Both sides are computed over the same set of locations (internal usage,
        allowed companies). Using ``qty_available`` for the balance would not be
        comparable: it only counts internal locations sitting under a warehouse
        view location, so stock in an internal location outside a warehouse tree
        would show up in the moves and not in the total, and the check would
        report a gap that isn't there.
        """
        if not product_id:
            raise UserError("product_id is required for ledger_check().")
        product = self._access("product.product").browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)

        Move = self._access("stock.move")
        base = [("product_id", "=", product.id), ("state", "=", "done")]
        moved_in = sum(
            Move.search(
                base
                + [
                    ("location_dest_id.usage", "=", "internal"),
                    ("location_id.usage", "!=", "internal"),
                ]
                + self._company_domain()
            ).mapped("quantity")
        )
        moved_out = sum(
            Move.search(
                base
                + [
                    ("location_id.usage", "=", "internal"),
                    ("location_dest_id.usage", "!=", "internal"),
                ]
                + self._company_domain()
            ).mapped("quantity")
        )
        expected = moved_in - moved_out
        # Sum the quants over the same location set the moves were counted
        # over, so the two sides are actually comparable (see the docstring).
        actual = sum(
            self._access("stock.quant")
            .search([("product_id", "=", product.id), ("location_id.usage", "=", "internal")] + self._company_domain())
            .mapped("quantity")
        )
        gap = actual - expected
        balanced = abs(gap) < 0.001
        result = {
            "moves_in": moved_in,
            "moves_out": moved_out,
            "expected_from_moves": expected,
            "actual_in_quants": actual,
            "gap": gap,
            "balanced": balanced,
        }
        if not balanced:
            result["warning"] = (
                "DATA INCONSISTENCY: the quants hold %s but the move ledger only "
                "accounts for %s (gap %s). This is not a normal reconciliation "
                "cause - no operator action can produce it, because on-hand is a "
                "readonly field that Odoo only changes by journalling a move. It "
                "means some code wrote stock.quant.quantity directly. Report the "
                "inconsistency itself as the finding and escalate to a human; do "
                "not try to explain the Shopify difference from it." % (actual, expected, gap)
            )
        return result

    @api.model
    def stock_by_location(self, product_id):
        """Where this product's stock physically sits, per location.

        Answers "was it moved to another warehouse?". Odoo's headline on-hand
        sums every internal location, so a transfer between warehouses leaves
        the total unchanged while Shopify - which is usually fed from one
        location, or from a subset - sees something quite different. A
        divergence with a clean move history often turns out to be this.
        """
        if not product_id:
            raise UserError("product_id is required for stock_by_location().")
        product = self._access("product.product").browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)
        quants = self._access("stock.quant").search(
            [
                ("product_id", "=", product.id),
                ("location_id.usage", "in", ["internal", "transit"]),
            ]
            + self._company_domain(),
            limit=MAX_LIMIT,
        )
        rows = []
        for quant in quants:
            location = quant.location_id
            rows.append(
                {
                    "location": location.display_name,
                    "location_id": location.id,
                    "location_usage": location.usage,
                    "warehouse": location.warehouse_id.display_name or None,
                    "quantity": quant.quantity,
                    "reserved": quant.reserved_quantity,
                    "available": quant.quantity - quant.reserved_quantity,
                    "last_count_date": str(quant.last_count_date) if quant.last_count_date else None,
                }
            )
        rows.sort(key=lambda r: r["quantity"], reverse=True)
        return {
            "product": {"id": product.id, "sku": product.default_code or product.barcode},
            "total_on_hand": product.qty_available,
            "locations": rows,
        }

    @api.model
    def move_details(self, move_ids):
        """Return the full picture for specific ``stock.move`` records.

        The investigation loop's follow-up question after a discrepancy
        snapshot is almost always "what is actually going on with *that* move" -
        the aged delivery, the receipt that never landed. This returns the
        picking's own state and dates alongside the move's, which is what
        separates "shipped but never validated in Odoo" from "genuinely still
        in the warehouse".
        """
        ids = [int(i) for i in (move_ids or [])][:MAX_LIMIT]
        if not ids:
            return []
        moves = self._access("stock.move").search([("id", "in", ids)] + self._company_domain())
        rows = self._serialize_moves(moves)
        by_id = {m.id: m for m in moves}
        for row in rows:
            move = by_id[row["id"]]
            picking = move.picking_id
            row.update(
                {
                    "location_from_usage": move.location_id.usage,
                    "location_to_usage": move.location_dest_id.usage,
                    "warehouse_from": move.location_id.warehouse_id.display_name or None,
                    "warehouse_to": move.location_dest_id.warehouse_id.display_name or None,
                    "picking": {
                        "name": picking.name,
                        "state": picking.state,
                        "scheduled_date": str(picking.scheduled_date) if picking.scheduled_date else None,
                        "date_done": str(picking.date_done) if picking.date_done else None,
                        "partner": picking.partner_id.display_name or None,
                        "backorder_of": picking.backorder_id.name or None,
                    }
                    if picking
                    else None,
                }
            )
        return rows

    @api.model
    def shopify_orders_for_sku(self, product_id, limit=20, since_days=30):
        """Recent Shopify orders containing this product's SKU, newest first.

        The counterpart to :meth:`sale_order_lines`: comparing the two is what
        identifies a Shopify sale that Odoo never recorded, which is the only
        evidence that can justify the ``create_missing_sale_order`` resolution.

        Read-only and degrades gracefully - a Shopify outage returns an error
        row rather than raising, so the investigation continues on Odoo-side
        evidence alone.
        """
        if not product_id:
            raise UserError("product_id is required for shopify_orders_for_sku().")
        product = self._access("product.product").browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)
        sku = product.default_code or product.barcode
        if not sku:
            return {"error": "Product %s has no SKU/barcode to match in Shopify." % product.display_name}
        try:
            return self._shopify_client().list_orders_for_sku(
                sku, limit=self._clamp_limit(limit), since_days=since_days
            )
        except Exception as exc:  # noqa: BLE001 - degrade gracefully for the agent
            _logger.warning("AI Ops: Shopify order lookup failed for %s: %s", sku, exc)
            return {"error": "Shopify order lookup failed: %s" % exc}

    @api.model
    def sale_order_lines(self, product_id, limit=20, only_undelivered=False):
        """Return sale order lines for a product, newest first.

        ``discrepancy_context`` already returns the 20 most recent lines; this
        exists so the investigation can widen the window or filter to the lines
        that actually explain a divergence (ordered but never delivered).
        """
        if not product_id:
            raise UserError("product_id is required for sale_order_lines().")
        if "sale.order.line" not in self.env:
            return []
        domain = [("product_id", "=", int(product_id))] + self._company_domain()
        lines = self._access("sale.order.line").search(domain, order="create_date desc", limit=self._clamp_limit(limit))
        rows = []
        for line in lines:
            undelivered = line.product_uom_qty - line.qty_delivered
            if only_undelivered and undelivered <= 0:
                continue
            order = line.order_id
            rows.append(
                {
                    "order": order.name,
                    "state": order.state,
                    "date_order": str(order.date_order) if order.date_order else None,
                    "qty_ordered": line.product_uom_qty,
                    "qty_delivered": line.qty_delivered,
                    "undelivered": undelivered,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # 3. Write an inventory adjustment patch
    # ------------------------------------------------------------------
    @api.model
    def apply_inventory_patch(self, product_id, counted_qty, location_id=None, reason=None, task_id=None):
        """Apply an inventory adjustment so on-hand quantity == ``counted_qty``.

        Uses the Odoo 19 ``stock.quant`` inventory-adjustment flow: set
        ``inventory_quantity`` on the (product, location) quant and apply it.
        Returns a summary dict with the resulting on-hand quantity.

        This is the agent's write-back path. When called by the agent user,
        ``task_id`` must reference an ``ai.ops.task`` with a persisted
        *approve* decision (see :meth:`_require_approved_task`).
        """
        if not product_id:
            raise UserError("product_id is required for apply_inventory_patch().")
        self._require_approved_task(task_id, product_id)
        try:
            counted_qty = float(counted_qty)
        except (TypeError, ValueError) as exc:
            raise ValidationError("counted_qty must be a number.") from exc

        product = self._access("product.product").browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)

        if location_id:
            location = self._access("stock.location").browse(int(location_id))
            if not location.exists():
                raise UserError("Unknown location_id %s." % location_id)
            if location.company_id and location.company_id.id not in self.env.companies.ids:
                raise UserError("Location %s belongs to another company." % location.display_name)
        else:
            # Default to the company's main internal stock location.
            warehouse = self._access("stock.warehouse").search([("company_id", "=", self.env.company.id)], limit=1)
            location = warehouse.lot_stock_id
        if not location:
            raise UserError("Could not resolve a stock location for the adjustment.")

        # Elevated: the agent is denied direct quant writes by record rule, so
        # this gated method is the only route to an inventory adjustment. The
        # approval gate above has already run. inventory_mode still resolves
        # against the *real* user, who holds stock.group_stock_user.
        Quant = self._access("stock.quant").with_context(inventory_mode=True)
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
    @api.model
    def _shopify_location(self):
        """The Odoo location whose stock backs the Shopify channel, if declared."""
        raw = self.env["res.config.settings"]._ai_ops_get_param("odoo_ai_ops.shopify_stock_location_id")
        if not raw:
            return self.env["stock.location"].browse()
        try:
            location = self._access("stock.location").browse(int(raw))
        except (TypeError, ValueError):
            return self.env["stock.location"].browse()
        return location if location.exists() else self.env["stock.location"].browse()

    @api.model
    def _odoo_on_hand_for_shopify(self, product):
        """On-hand to compare against Shopify, plus how it was scoped.

        Comparing Odoo's *total* on-hand against Shopify only works when every
        internal location feeds Shopify. In the ordinary shop + warehouse setup
        it does not: Odoo sums both, Shopify sees only what the shop stocks, so
        Odoo looks permanently higher. Left unscoped, the analysis reads that
        standing gap as a Shopify undercount and "corrects" Shopify upward -
        overselling stock that is sitting in the back warehouse.

        So when a backing location is configured we compare that subtree; when
        it is not, we say so, and say whether the totals can be trusted (they
        can, if only one location actually holds stock).
        """
        location = self._shopify_location()
        total = product.qty_available
        if location:
            scoped = product.with_context(location=location.id).qty_available
            return scoped, {
                "configured": True,
                "location": location.display_name,
                "location_id": location.id,
                "odoo_total_all_locations": total,
                "note": (
                    "Compared %s only (child locations included). Odoo holds %s in "
                    "total across all locations; the rest does not feed Shopify." % (location.display_name, total)
                ),
            }

        stocked = self._access("stock.quant").search_count(
            [
                ("product_id", "=", product.id),
                ("location_id.usage", "=", "internal"),
                ("quantity", "!=", 0),
            ]
            + self._company_domain()
        )
        scope = {
            "configured": False,
            "location": None,
            "odoo_total_all_locations": total,
            "stocked_internal_locations": stocked,
        }
        if stocked > 1:
            scope["warning"] = (
                "AMBIGUOUS COMPARISON: no Shopify stock location is configured and "
                "this product is stocked in %s internal locations, so Odoo's total "
                "may include stock that never feeds Shopify (e.g. a back warehouse). "
                "The difference below may not be a real discrepancy. Do NOT recommend "
                "update_shopify on this evidence - check stock_by_location and "
                "recommend a human set the Shopify Stock Location in AI Ops settings."
            ) % stocked
        else:
            scope["note"] = (
                "No Shopify stock location configured, but stock sits in at most one "
                "internal location, so the totals are comparable."
            )
        return total, scope

    def _shopify_client(self):
        settings = self.env["res.config.settings"]
        return ShopifyClient(
            shop_domain=settings._ai_ops_get_param("odoo_ai_ops.shopify_shop_domain"),
            admin_token=settings._ai_ops_get_param("odoo_ai_ops.shopify_admin_token"),
            api_version=settings._ai_ops_get_param("odoo_ai_ops.shopify_api_version", "2026-07"),
        )

    @api.model
    def discrepancy_context(self, product_id, fetch_shopify=True, stale_days=3, history_days=30):
        """Gather everything the agent needs to explain a stock discrepancy.

        Returns Odoo on-hand vs. Shopify available, plus the evidence that
        usually explains a divergence, one bucket per cause:

        * ``pending_outgoing_moves`` - a shipment sent but never validated.
        * ``pending_incoming_moves`` - a receipt that was never recorded.
        * ``pending_internal_moves`` - a transfer between locations left open.
        * ``recent_inventory_adjustments`` - someone forced the on-hand count,
          with who did it and the reason they gave.
        * ``stock_by_location`` - where the stock actually sits, which is how a
          "moved to another warehouse" case shows up at all (the total is
          unchanged, so nothing else would reveal it).
        * ``recent_sale_orders`` - a missing or undelivered order.

        This is the deterministic evidence floor, so the agent starts from all
        of the common causes rather than having to guess which to ask about.
        """
        if not product_id:
            raise UserError("product_id is required for discrepancy_context().")
        product = self._access("product.product").browse(int(product_id))
        if not product.exists():
            raise UserError("Unknown product_id %s." % product_id)

        Move = self._access("stock.move")
        now = fields.Datetime.now()
        # Two windows: `stale_days` is "an open move this old is suspicious",
        # while adjustments need a wider lookback - one made a fortnight ago
        # still explains today's divergence.
        stale_cutoff = fields.Datetime.subtract(now, days=int(stale_days))
        history_cutoff = fields.Datetime.subtract(now, days=int(history_days))

        def _serialize(moves):
            return self._serialize_moves(moves, stale_cutoff=stale_cutoff)

        pending_out = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "in", OPEN_MOVE_STATES),
                ("location_dest_id.usage", "=", "customer"),
            ]
            + self._company_domain(),
            order="date",
            limit=50,
        )
        pending_in = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "in", OPEN_MOVE_STATES),
                ("location_id.usage", "=", "supplier"),
            ]
            + self._company_domain(),
            order="date",
            limit=50,
        )
        # An open transfer between two internal locations does not change total
        # on-hand, but it does move stock away from whichever location feeds
        # Shopify - and a stuck one leaves the goods in limbo.
        pending_internal = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "in", OPEN_MOVE_STATES),
                ("location_id.usage", "in", ["internal", "transit"]),
                ("location_dest_id.usage", "in", ["internal", "transit"]),
            ]
            + self._company_domain(),
            order="date",
            limit=50,
        )
        # "Someone forcefully changed the on-hand quantity" is one of the most
        # common real causes, and nothing else in this snapshot would reveal it.
        # _serialize_moves carries the author and the adjustment reason.
        recent_adjustments = Move.search(
            [
                ("product_id", "=", product.id),
                ("state", "=", "done"),
                ("is_inventory", "=", True),
                ("date", ">=", history_cutoff),
            ]
            + self._company_domain(),
            order="date desc",
            limit=20,
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

        # Scope the Odoo side to whatever actually backs Shopify, so the two
        # numbers describe the same stock (see _odoo_on_hand_for_shopify).
        on_hand, location_scope = self._odoo_on_hand_for_shopify(product)
        discrepancy = (on_hand - shopify_qty) if shopify_qty is not None else None

        # Recent sales orders (only if the 'sale' app is installed).
        recent_sale_orders = self.sale_order_lines(product.id, limit=20)

        return {
            "product": {"id": product.id, "sku": sku, "name": product.display_name},
            "odoo_on_hand": on_hand,
            "location_scope": location_scope,
            "odoo_free_qty": product.free_qty,
            "odoo_incoming": product.incoming_qty,
            "odoo_outgoing": product.outgoing_qty,
            "shopify_available": shopify_qty,
            "shopify_error": shopify_error,
            "discrepancy_odoo_minus_shopify": discrepancy,
            "pending_outgoing_moves": _serialize(pending_out),
            "pending_incoming_moves": _serialize(pending_in),
            "pending_internal_moves": _serialize(pending_internal),
            "recent_inventory_adjustments": self._serialize_moves(recent_adjustments),
            "stock_by_location": self.stock_by_location(product.id)["locations"],
            "ledger_check": self.ledger_check(product.id),
            "recent_sale_orders": recent_sale_orders,
            "stale_days": int(stale_days),
            "history_days": int(history_days),
        }

    # ------------------------------------------------------------------
    # 5. Push Odoo's on-hand back to Shopify (Odoo is source of truth)
    # ------------------------------------------------------------------
    @api.model
    def push_inventory_to_shopify(self, product_id, qty, reason=None, task_id=None):
        """Set Shopify's available quantity for a product to ``qty``.

        Used for the "Odoo has more stock — Shopify undercount / human error"
        resolution: Odoo is authoritative, so we correct Shopify. When called
        by the agent user, ``task_id`` must reference an ``ai.ops.task`` with a
        persisted *approve* decision (see :meth:`_require_approved_task`).
        """
        if not product_id:
            raise UserError("product_id is required for push_inventory_to_shopify().")
        self._require_approved_task(task_id, product_id)
        product = self._access("product.product").browse(int(product_id))
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
