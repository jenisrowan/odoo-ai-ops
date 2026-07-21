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
from datetime import date, timedelta

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

# Recent orders containing a SKU. This is the evidence for the "Shopify sold it
# but Odoo never recorded the sale" case: if Shopify shows a paid order for the
# SKU with no matching Odoo sale order, Odoo is overstating its on-hand.
# `sku:` is a supported filter on the orders search (Admin API 2026-07); the
# line items still have to be filtered client-side because an order matches on
# any of its SKUs and we only want the one under investigation.
_ORDERS_BY_SKU_QUERY = """
query OrdersBySku($q: String!, $first: Int!) {
  orders(first: $first, query: $q, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        id
        name
        createdAt
        cancelledAt
        displayFinancialStatus
        displayFulfillmentStatus
        lineItems(first: 50) {
          edges {
            node {
              sku
              name
              quantity
              currentQuantity
              unfulfilledQuantity
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


# --- Webhook subscription management (Admin API) ---------------------------
# Shopify's web UI only offers Pub/Sub or EventBridge destinations for a custom
# app; plain HTTPS webhooks must be registered through the Admin API. These are
# the only two topics the AI Ops pipeline ingests. The keys are GraphQL
# ``WebhookSubscriptionTopic`` enum values; the values are the dotted topics
# Shopify sends in the ``X-Shopify-Topic`` header (what the agent routes on).
WEBHOOK_TOPICS = {
    "ORDERS_CREATE": "orders/create",
    "ORDERS_RISK_ASSESSMENT_CHANGED": "orders/risk_assessment_changed",
}

_WEBHOOK_LIST_QUERY = """
query WebhookSubscriptions($cursor: String) {
  webhookSubscriptions(first: 100, after: $cursor) {
    edges {
      node {
        id
        topic
        endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_WEBHOOK_CREATE_MUTATION = """
mutation WebhookCreate($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""

_WEBHOOK_UPDATE_MUTATION = """
mutation WebhookUpdate($id: ID!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionUpdate(id: $id, webhookSubscription: $sub) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""


_SHOP_QUERY = """
query { shop { name myshopifyDomain ianaTimezone } }
"""


# Pull Shopify's own fraud analysis for an order. The
# orders/risk_assessment_changed webhook carries only the summarised risk level;
# the underlying signals the fraud workflow needs live here:
#   * risk.assessments[].facts - the "why" behind the level, each tagged with a
#     NEGATIVE/NEUTRAL/POSITIVE sentiment; already encode Shopify's IP/proxy
#     geolocation and order-velocity checks, so we do not reconstruct those.
#   * customer.numberOfOrders / amountSpent - order history (a brand-new account
#     is a strong fraud signal; supersedes the deprecated ordersCount/totalSpent).
#   * transactions[].paymentDetails - card AVS/CVV verification results.
# The legacy REST Order Risk resource was deprecated in 2024-04; this GraphQL
# OrderRiskAssessment API is its replacement. Needs the read_orders scope.
_ORDER_RISK_CONTEXT_QUERY = """
query OrderRiskContext($id: ID!) {
  order(id: $id) {
    risk {
      recommendation
      assessments {
        riskLevel
        provider { title }
        facts { description sentiment }
      }
    }
    customer {
      numberOfOrders
      amountSpent { amount currencyCode }
      verifiedEmail
      createdAt
    }
    transactions(first: 10) {
      kind
      status
      gateway
      paymentDetails {
        ... on CardPaymentDetails {
          avsResultCode
          cvvResultCode
          bin
          company
        }
      }
    }
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

    def get_shop_info(self):
        """Return basic shop info; doubles as a credentials/connectivity check.

        Raises ShopifyError if the token/domain are wrong (bad auth surfaces as an
        HTTP 401/403 or a GraphQL error), so callers can use it as a ping.
        """
        data = self._execute(_SHOP_QUERY, {})
        return (data or {}).get("shop") or {}

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
    # Fraud analysis context (used by the fraud-validation workflow)
    # ------------------------------------------------------------------
    def get_order_risk_context(self, order_id):
        """Return Shopify's own fraud signals for an order as a flat dict.

        Bundles the risk-assessment facts (with sentiment), the customer's order
        history, and card AVS/CVV verification - the signals Shopify already
        computed to flag the order, none of which the risk webhook carries. Shape::

            {"recommendation": "INVESTIGATE",
             "assessments": [{"risk_level": "HIGH", "provider": "Shopify",
                              "facts": [{"description": "...", "sentiment": "NEGATIVE"}]}],
             "customer_history": {"number_of_orders": "0", "amount_spent": "0.00",
                                  "currency": "USD", "verified_email": False,
                                  "created_at": "..."},
             "payment_verification": [{"kind": "SALE", "status": "SUCCESS",
                                       "avs_result": "N", "cvv_result": "N",
                                       "card_company": "Visa", "bin": "424242"}]}

        Returns ``{}`` if the order is unknown to Shopify.
        """
        data = self._execute(_ORDER_RISK_CONTEXT_QUERY, {"id": self.to_gid(order_id)})
        order = (data or {}).get("order") or {}
        if not order:
            return {}

        risk = order.get("risk") or {}
        assessments = [
            {
                "risk_level": a.get("riskLevel"),
                # provider is null when the assessment is Shopify's own.
                "provider": (a.get("provider") or {}).get("title") or "Shopify",
                "facts": [
                    {"description": f.get("description"), "sentiment": f.get("sentiment")}
                    for f in (a.get("facts") or [])
                ],
            }
            for a in (risk.get("assessments") or [])
        ]

        customer = order.get("customer") or {}
        amount_spent = customer.get("amountSpent") or {}

        payment = []
        for txn in order.get("transactions") or []:
            details = txn.get("paymentDetails") or {}
            # Only card transactions carry AVS/CVV; skip gift-card/other rows.
            if details.get("avsResultCode") is None and details.get("cvvResultCode") is None:
                continue
            payment.append(
                {
                    "kind": txn.get("kind"),
                    "status": txn.get("status"),
                    "avs_result": details.get("avsResultCode"),
                    "cvv_result": details.get("cvvResultCode"),
                    "card_company": details.get("company"),
                    "bin": details.get("bin"),
                }
            )

        return {
            "recommendation": risk.get("recommendation"),
            "assessments": assessments,
            "customer_history": {
                "number_of_orders": customer.get("numberOfOrders"),
                "amount_spent": amount_spent.get("amount"),
                "currency": amount_spent.get("currencyCode"),
                "verified_email": customer.get("verifiedEmail"),
                "created_at": customer.get("createdAt"),
            }
            if customer
            else {},
            "payment_verification": payment,
        }

    # ------------------------------------------------------------------
    # Webhook subscriptions (registered from the Odoo Settings UI)
    # ------------------------------------------------------------------
    def list_webhooks(self):
        """Return all HTTPS webhook subscriptions as ``[{id, topic, callback_url}]``."""
        subs = []
        cursor = None
        while True:
            data = self._execute(_WEBHOOK_LIST_QUERY, {"cursor": cursor})
            conn = (data or {}).get("webhookSubscriptions") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node") or {}
                endpoint = node.get("endpoint") or {}
                subs.append(
                    {
                        "id": node.get("id"),
                        "topic": node.get("topic"),
                        "callback_url": endpoint.get("callbackUrl"),
                    }
                )
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage"):
                cursor = page.get("endCursor")
            else:
                return subs

    def _create_webhook(self, topic, callback_url):
        data = self._execute(
            _WEBHOOK_CREATE_MUTATION,
            {"topic": topic, "sub": {"callbackUrl": callback_url, "format": "JSON"}},
        )
        result = (data or {}).get("webhookSubscriptionCreate") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise ShopifyError("Shopify refused webhook create for %s: %s" % (topic, errors))
        return (result.get("webhookSubscription") or {}).get("id")

    def _update_webhook(self, sub_id, callback_url):
        data = self._execute(
            _WEBHOOK_UPDATE_MUTATION,
            {"id": sub_id, "sub": {"callbackUrl": callback_url, "format": "JSON"}},
        )
        result = (data or {}).get("webhookSubscriptionUpdate") or {}
        errors = result.get("userErrors") or []
        if errors:
            raise ShopifyError("Shopify refused webhook update for %s: %s" % (sub_id, errors))
        return (result.get("webhookSubscription") or {}).get("id")

    def sync_webhooks(self, callback_url, topics=None):
        """Idempotently ensure one HTTPS webhook per topic points at ``callback_url``.

        For each required topic: leave it alone if a subscription already targets
        ``callback_url``; re-point an existing subscription that targets a stale
        URL; otherwise create it. Safe to run repeatedly (e.g. after the edge URL
        changes). Returns ``{"created": [...], "updated": [...], "unchanged": [...]}``
        keyed by the dotted topic name.
        """
        if not callback_url:
            raise ShopifyError("Webhook callback URL is not configured.")
        topics = topics or list(WEBHOOK_TOPICS)

        by_topic = {}
        for sub in self.list_webhooks():
            by_topic.setdefault(sub["topic"], []).append(sub)

        summary = {"created": [], "updated": [], "unchanged": []}
        for topic in topics:
            dotted = WEBHOOK_TOPICS.get(topic, topic)
            existing = by_topic.get(topic) or []
            if any(s.get("callback_url") == callback_url for s in existing):
                summary["unchanged"].append(dotted)
            elif existing:
                self._update_webhook(existing[0]["id"], callback_url)
                summary["updated"].append(dotted)
            else:
                self._create_webhook(topic, callback_url)
                summary["created"].append(dotted)
        return summary

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

    def list_orders_for_sku(self, sku, limit=20, since_days=30):
        """Return recent Shopify orders containing ``sku``, newest first.

        Answers "did Shopify sell this that Odoo never heard about?" — the
        evidence behind the ``create_missing_sale_order`` resolution. Each row
        carries only the line items for the requested SKU, plus the order's
        financial and fulfillment status so a cancelled or unpaid order is not
        mistaken for a real missing sale.

        :param since_days: how far back to look; ``None`` for no date bound.
        """
        if not sku:
            return []
        terms = ['sku:"%s"' % str(sku).replace('"', "")]
        if since_days:
            # date/timedelta, not odoo.fields: this client is deliberately free
            # of Odoo imports so it stays unit-testable in isolation.
            cutoff = date.today() - timedelta(days=int(since_days))
            terms.append("created_at:>=%s" % cutoff.isoformat())
        data = self._execute(
            _ORDERS_BY_SKU_QUERY,
            {"q": " ".join(terms), "first": max(1, min(int(limit), 100))},
        )
        rows = []
        for edge in ((data or {}).get("orders") or {}).get("edges") or []:
            node = edge.get("node") or {}
            lines = [
                {
                    "sku": ln.get("sku"),
                    "name": ln.get("name"),
                    "qty_ordered": ln.get("quantity"),
                    # Excludes refunded/removed units - the number that actually
                    # left the shelf.
                    "qty_current": ln.get("currentQuantity"),
                    "qty_unfulfilled": ln.get("unfulfilledQuantity"),
                }
                for ln in (
                    e.get("node") or {}
                    for e in ((node.get("lineItems") or {}).get("edges") or [])
                )
                if ln.get("sku") == sku
            ]
            if not lines:
                continue
            rows.append(
                {
                    "order": node.get("name"),
                    "created_at": node.get("createdAt"),
                    "cancelled_at": node.get("cancelledAt"),
                    "financial_status": node.get("displayFinancialStatus"),
                    "fulfillment_status": node.get("displayFulfillmentStatus"),
                    "line_items": lines,
                }
            )
        return rows

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
