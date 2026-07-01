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


class ShopifyError(Exception):
    """Raised on transport failures or GraphQL/user errors from Shopify."""


class ShopifyClient:
    def __init__(self, shop_domain, admin_token, api_version, timeout=DEFAULT_TIMEOUT):
        if not shop_domain or not admin_token:
            raise ShopifyError("Shopify credentials are not configured.")
        self.shop_domain = shop_domain.replace("https://", "").replace("http://", "").strip("/")
        self.admin_token = admin_token
        self.api_version = api_version or "2025-01"
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

    def cancel_order(self, order_id, reason="FRAUD", refund=True, restock=True, staff_note=None):
        """Cancel an order in Shopify. Returns the job handle on success.

        :param reason: One of Shopify's ``OrderCancelReason`` enum values
                       (CUSTOMER, FRAUD, INVENTORY, DECLINED, STAFF, OTHER).
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
