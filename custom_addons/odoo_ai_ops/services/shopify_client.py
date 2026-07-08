# -*- coding: utf-8 -*-
"""Minimal Shopify Admin GraphQL client.

Only the operations the AI Ops gatekeeper actually needs are implemented:

* ``cancel_order`` - used both by the cheap-order auto-rejection rule and by the
  final "Reject & Cancel" manager decision. Shopify deprecated the REST
  ``POST /orders/{id}/cancel`` endpoint in favour of the ``orderCancel``
  GraphQL mutation, which is what we call here.

The client deliberately has no Odoo dependencies so it can be unit-tested in
isolation; credentials are passed in by the caller (resolved from
``ir.config_parameter`` / the environment).
"""

import logging

import requests

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5, 20)

# ``orderCancel`` returns a job handle plus userErrors. We surface userErrors so
# the caller can record exactly why Shopify refused a cancellation.
_ORDER_CANCEL_MUTATION = """
mutation OrderCancel(
  $orderId: ID!
  $reason: OrderCancelReason!
  $refund: Boolean!
  $restock: Boolean!
  $staffNote: String
) {
  orderCancel(
    orderId: $orderId
    reason: $reason
    refund: $refund
    restock: $restock
    staffNote: $staffNote
  ) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}
"""


# Look up an inventory item (and its stocked levels) by SKU.
_INVENTORY_BY_SKU_QUERY = """
query InventoryBySku($q: String!) {
  inventoryItems(first: 5, query: $q) {
    edges {
      node {
        id
        sku
        inventoryLevels(first: 20) {
          edges {
            node {
              location { id name }
              quantities(names: ["available"]) { name quantity }
            }
          }
        }
      }
    }
  }
}
"""

# Set the "available" quantity of an inventory item at a location.
_INVENTORY_SET_MUTATION = """
mutation SetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message code }
  }
}
"""


class ShopifyError(Exception):
    """Raised on transport failures or GraphQL/user errors from Shopify."""


class ShopifyClient:
    def __init__(self, shop_domain, admin_token, api_version, timeout=DEFAULT_TIMEOUT):
        if not shop_domain or not admin_token:
            raise ShopifyError("Shopify credentials are not configured.")
        self.shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
        self.admin_token = admin_token
        self.api_version = api_version or "2026-07"
        self.timeout = timeout

    @property
    def endpoint(self):
        return "https://%s/admin/api/%s/graphql.json" % (self.shop_domain, self.api_version)

    @staticmethod
    def to_gid(order_id):
        """Normalise a raw numeric order id to a Shopify global id (GID)."""
        order_id = str(order_id)
        if order_id.startswith("gid://"):
            return order_id
        return "gid://shopify/Order/%s" % order_id

    def _execute(self, query, variables):
        headers = {
            "X-Shopify-Access-Token": self.admin_token,
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self.endpoint,
                json={"query": query, "variables": variables},
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ShopifyError("Shopify request failed: %s" % exc) from exc

        if response.status_code >= 400:
            raise ShopifyError("Shopify HTTP %s: %s" % (response.status_code, response.text[:500]))
        body = response.json()
        # Top-level GraphQL errors (bad query, throttling, auth, …).
        if body.get("errors"):
            raise ShopifyError("Shopify GraphQL errors: %s" % body["errors"])
        return body.get("data", {})

    def cancel_order(self, order_id, reason="FRAUD", refund=False, restock=True, staff_note=None):
        """Cancel an order in Shopify. Returns the job handle on success.

        :param reason: One of Shopify's ``OrderCancelReason`` enum values
                       (CUSTOMER, FRAUD, INVENTORY, DECLINED, STAFF, OTHER).
        :param refund: Whether Shopify should refund the payment as part of the
                       cancellation. Defaults to ``False``: on a fraud rejection
                       an automatic refund is usually NOT wanted (the payment
                       should be voided/reviewed per the store's fraud process),
                       so refunding is an explicit opt-in
                       (``odoo_ai_ops.refund_on_cancel``).
        """
        data = self._execute(
            _ORDER_CANCEL_MUTATION,
            {
                "orderId": self.to_gid(order_id),
                "reason": reason,
                "refund": refund,
                "restock": restock,
                "staffNote": staff_note or "Cancelled by Odoo AI Ops",
            },
        )
        result = (data or {}).get("orderCancel") or {}
        user_errors = result.get("orderCancelUserErrors") or []
        if user_errors:
            raise ShopifyError("Shopify refused cancellation: %s" % user_errors)
        return result.get("job") or {}

    # ------------------------------------------------------------------
    # Inventory (used by the reconciliation root-cause analysis)
    # ------------------------------------------------------------------
    def _find_inventory_item(self, sku):
        """Return the first Shopify inventory item node matching ``sku`` (or None)."""
        data = self._execute(_INVENTORY_BY_SKU_QUERY, {"q": "sku:%s" % sku})
        edges = ((data or {}).get("inventoryItems") or {}).get("edges") or []
        return edges[0]["node"] if edges else None

    def get_available_inventory(self, sku):
        """Return the total 'available' quantity across all locations for ``sku``.

        Returns ``None`` if no inventory item matches the SKU.
        """
        node = self._find_inventory_item(sku)
        if not node:
            return None
        total = 0.0
        for lvl in (node.get("inventoryLevels") or {}).get("edges", []):
            for q in lvl["node"].get("quantities") or []:
                if q.get("name") == "available":
                    total += float(q.get("quantity") or 0)
        return total

    def set_inventory_quantity(self, sku, qty, reason="correction", location_id=None):
        """Set the 'available' quantity for ``sku`` at a location.

        Used when Odoo is the source of truth (e.g. a Shopify undercount caused
        by human error) and we push Odoo's on-hand back to Shopify.
        """
        node = self._find_inventory_item(sku)
        if not node:
            raise ShopifyError("No Shopify inventory item found for SKU %s" % sku)
        levels = (node.get("inventoryLevels") or {}).get("edges", [])
        if location_id is None:
            if not levels:
                raise ShopifyError("Inventory item %s has no stocked location." % sku)
            location_id = levels[0]["node"]["location"]["id"]

        data = self._execute(
            _INVENTORY_SET_MUTATION,
            {
                "input": {
                    "name": "available",
                    "reason": reason,
                    "ignoreCompareQuantity": True,
                    "quantities": [
                        {
                            "inventoryItemId": node["id"],
                            "locationId": location_id,
                            "quantity": int(round(float(qty))),
                        }
                    ],
                }
            },
        )
        result = (data or {}).get("inventorySetQuantities") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise ShopifyError("Shopify refused inventory set: %s" % user_errors)
        return {"sku": sku, "location_id": location_id, "quantity": int(round(float(qty)))}
