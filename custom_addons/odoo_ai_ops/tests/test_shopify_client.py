# -*- coding: utf-8 -*-
"""Unit tests for the Shopify Admin GraphQL client.

``requests`` is mocked, so nothing hits Shopify. Covers order cancellation
(used by the fraud bypass + reject path) and the inventory read/write used by
the reconciliation root-cause analysis.
"""

from unittest.mock import MagicMock, patch

from odoo.addons.odoo_ai_ops.services.shopify_client import ShopifyClient, ShopifyError
from odoo.tests import TransactionCase, tagged

_PATH = "odoo.addons.odoo_ai_ops.services.shopify_client.requests.post"


def _resp(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    return r


_URL = "https://edge.example/webhooks/shopify"


def _wh_list(nodes):
    return _resp(
        {
            "data": {
                "webhookSubscriptions": {
                    "edges": [{"node": n} for n in nodes],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    )


def _wh_write_ok(mutation):
    return _resp(
        {
            "data": {
                mutation: {
                    "webhookSubscription": {"id": "gid://shopify/WebhookSubscription/9"},
                    "userErrors": [],
                }
            }
        }
    )


def _wh_create_ok():
    return _wh_write_ok("webhookSubscriptionCreate")


def _wh_update_ok():
    return _wh_write_ok("webhookSubscriptionUpdate")


@tagged("post_install", "-at_install", "ai_ops")
class TestShopifyClient(TransactionCase):
    def _client(self):
        return ShopifyClient(shop_domain="test.myshopify.com", admin_token="tok", api_version="2026-07")

    @patch(_PATH)
    def test_cancel_order_normalizes_gid_and_returns_job(self, mock_post):
        mock_post.return_value = _resp(
            {
                "data": {
                    "orderCancel": {"job": {"id": "gid://shopify/Job/1", "done": False}, "orderCancelUserErrors": []}
                }
            }
        )
        job = self._client().cancel_order("12345", reason="FRAUD")
        self.assertEqual(job["id"], "gid://shopify/Job/1")
        sent = mock_post.call_args.kwargs["json"]["variables"]
        self.assertEqual(sent["orderId"], "gid://shopify/Order/12345")
        self.assertEqual(sent["reason"], "FRAUD")
        # Refunding is an explicit opt-in; a fraud cancellation must not
        # auto-refund by default.
        self.assertFalse(sent["refund"])

    @patch(_PATH)
    def test_cancel_order_user_error_raises(self, mock_post):
        mock_post.return_value = _resp(
            {"data": {"orderCancel": {"job": None, "orderCancelUserErrors": [{"message": "cannot cancel"}]}}}
        )
        with self.assertRaises(ShopifyError):
            self._client().cancel_order("1")

    @patch(_PATH)
    def test_graphql_top_level_errors_raise(self, mock_post):
        mock_post.return_value = _resp({"errors": [{"message": "throttled"}]})
        with self.assertRaises(ShopifyError):
            self._client().cancel_order("1")

    # --- Fraud risk context -------------------------------------------
    @patch(_PATH)
    def test_get_order_risk_context_normalizes(self, mock_post):
        mock_post.return_value = _resp(
            {
                "data": {
                    "order": {
                        "risk": {
                            "recommendation": "INVESTIGATE",
                            "assessments": [
                                {
                                    "riskLevel": "HIGH",
                                    "provider": None,  # null -> Shopify's own assessment
                                    "facts": [
                                        {"description": "Billing address is high risk", "sentiment": "NEGATIVE"},
                                        {"description": "Card verified", "sentiment": "POSITIVE"},
                                    ],
                                }
                            ],
                        },
                        "customer": {
                            "numberOfOrders": "0",
                            "amountSpent": {"amount": "0.00", "currencyCode": "USD"},
                            "verifiedEmail": False,
                            "createdAt": "2026-07-16T00:00:00Z",
                        },
                        "transactions": [
                            {
                                "kind": "SALE",
                                "status": "SUCCESS",
                                "gateway": "bogus",
                                "paymentDetails": {
                                    "avsResultCode": "N",
                                    "cvvResultCode": "N",
                                    "bin": "424242",
                                    "company": "Visa",
                                },
                            },
                            # A non-card row (e.g. gift card) has no AVS/CVV -> dropped.
                            {"kind": "SALE", "status": "SUCCESS", "gateway": "gift_card", "paymentDetails": {}},
                        ],
                    }
                }
            }
        )
        ctx = self._client().get_order_risk_context("55501")
        # A null provider is normalised to Shopify; facts keep their sentiment.
        self.assertEqual(ctx["recommendation"], "INVESTIGATE")
        self.assertEqual(ctx["assessments"][0]["provider"], "Shopify")
        self.assertEqual(ctx["assessments"][0]["risk_level"], "HIGH")
        self.assertEqual(ctx["assessments"][0]["facts"][0]["sentiment"], "NEGATIVE")
        # Order history flags a brand-new, unverified account.
        self.assertEqual(ctx["customer_history"]["number_of_orders"], "0")
        self.assertFalse(ctx["customer_history"]["verified_email"])
        # Only the card transaction carrying AVS/CVV is surfaced.
        self.assertEqual(len(ctx["payment_verification"]), 1)
        self.assertEqual(ctx["payment_verification"][0]["avs_result"], "N")
        self.assertEqual(ctx["payment_verification"][0]["card_company"], "Visa")
        # The raw order id is normalised to a GID in the query variables.
        sent = mock_post.call_args.kwargs["json"]["variables"]
        self.assertEqual(sent["id"], "gid://shopify/Order/55501")

    @patch(_PATH)
    def test_get_order_risk_context_unknown_order_returns_empty(self, mock_post):
        mock_post.return_value = _resp({"data": {"order": None}})
        self.assertEqual(self._client().get_order_risk_context("nope"), {})

    @patch(_PATH)
    def test_get_available_inventory_sums_levels(self, mock_post):
        mock_post.return_value = _resp(
            {
                "data": {
                    "inventoryItems": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/InventoryItem/1",
                                    "sku": "SKU1",
                                    "inventoryLevels": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "location": {"id": "l1"},
                                                    "quantities": [{"name": "available", "quantity": 3}],
                                                }
                                            },
                                            {
                                                "node": {
                                                    "location": {"id": "l2"},
                                                    "quantities": [{"name": "available", "quantity": 5}],
                                                }
                                            },
                                        ]
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        )
        self.assertEqual(self._client().get_available_inventory("SKU1"), 8.0)

    @patch(_PATH)
    def test_get_available_inventory_missing_returns_none(self, mock_post):
        mock_post.return_value = _resp({"data": {"inventoryItems": {"edges": []}}})
        self.assertIsNone(self._client().get_available_inventory("NOPE"))

    @patch(_PATH)
    def test_set_inventory_quantity(self, mock_post):
        find = {
            "data": {
                "inventoryItems": {
                    "edges": [
                        {
                            "node": {
                                "id": "gid://shopify/InventoryItem/1",
                                "sku": "SKU1",
                                "inventoryLevels": {
                                    "edges": [
                                        {
                                            "node": {
                                                "location": {"id": "gid://shopify/Location/9"},
                                                "quantities": [{"name": "available", "quantity": 2}],
                                            }
                                        }
                                    ]
                                },
                            }
                        }
                    ]
                }
            }
        }
        set_ok = {
            "data": {"inventorySetQuantities": {"inventoryAdjustmentGroup": {"createdAt": "now"}, "userErrors": []}}
        }
        # set_inventory_quantity does two POSTs: find item, then set.
        mock_post.side_effect = [_resp(find), _resp(set_ok)]
        result = self._client().set_inventory_quantity("SKU1", 10)
        self.assertEqual(result["quantity"], 10)
        self.assertEqual(result["location_id"], "gid://shopify/Location/9")

    @patch(_PATH)
    def test_get_shop_info_returns_shop(self, mock_post):
        mock_post.return_value = _resp(
            {"data": {"shop": {"name": "Test Store", "myshopifyDomain": "test.myshopify.com"}}}
        )
        shop = self._client().get_shop_info()
        self.assertEqual(shop["name"], "Test Store")

    # --- Webhook subscription sync -------------------------------------
    @patch(_PATH)
    def test_sync_webhooks_creates_missing(self, mock_post):
        # No existing subscriptions -> both topics are created.
        mock_post.side_effect = [_wh_list([]), _wh_create_ok(), _wh_create_ok()]
        summary = self._client().sync_webhooks(_URL)
        self.assertEqual(set(summary["created"]), {"orders/create", "orders/risk_assessment_changed"})
        self.assertFalse(summary["updated"])
        self.assertFalse(summary["unchanged"])
        # First create carries the right topic enum, callback URL and JSON format.
        create_vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
        self.assertEqual(create_vars["topic"], "ORDERS_CREATE")
        self.assertEqual(create_vars["sub"]["callbackUrl"], _URL)
        self.assertEqual(create_vars["sub"]["format"], "JSON")

    @patch(_PATH)
    def test_sync_webhooks_idempotent_when_present(self, mock_post):
        nodes = [
            {"id": "1", "topic": "ORDERS_CREATE", "endpoint": {"callbackUrl": _URL}},
            {"id": "2", "topic": "ORDERS_RISK_ASSESSMENT_CHANGED", "endpoint": {"callbackUrl": _URL}},
        ]
        mock_post.side_effect = [_wh_list(nodes)]
        summary = self._client().sync_webhooks(_URL)
        self.assertEqual(set(summary["unchanged"]), {"orders/create", "orders/risk_assessment_changed"})
        # Only the list query is issued; no create/update writes.
        self.assertEqual(mock_post.call_count, 1)

    @patch(_PATH)
    def test_sync_webhooks_repoints_stale_url(self, mock_post):
        nodes = [
            {
                "id": "gid://shopify/WebhookSubscription/1",
                "topic": "ORDERS_CREATE",
                "endpoint": {"callbackUrl": "https://old/webhooks/shopify"},
            }
        ]
        mock_post.side_effect = [_wh_list(nodes), _wh_update_ok(), _wh_create_ok()]
        summary = self._client().sync_webhooks(_URL)
        self.assertEqual(summary["updated"], ["orders/create"])
        self.assertEqual(summary["created"], ["orders/risk_assessment_changed"])
        update_vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
        self.assertEqual(update_vars["id"], "gid://shopify/WebhookSubscription/1")
        self.assertEqual(update_vars["sub"]["callbackUrl"], _URL)

    @patch(_PATH)
    def test_sync_webhooks_user_error_raises(self, mock_post):
        create_err = _resp(
            {
                "data": {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": None,
                        "userErrors": [{"message": "bad url"}],
                    }
                }
            }
        )
        mock_post.side_effect = [_wh_list([]), create_err]
        with self.assertRaises(ShopifyError):
            self._client().sync_webhooks(_URL)

    def test_sync_webhooks_requires_callback_url(self):
        with self.assertRaises(ShopifyError):
            self._client().sync_webhooks("")

    # --- orders by SKU (missing-sale evidence) ----------------------------
    @staticmethod
    def _orders_resp(orders):
        return _resp(
            {
                "data": {
                    "orders": {
                        "edges": [
                            {
                                "node": {
                                    **o,
                                    "lineItems": {"edges": [{"node": ln} for ln in o.pop("_lines", [])]},
                                }
                            }
                            for o in orders
                        ]
                    }
                }
            }
        )

    @patch(_PATH)
    def test_list_orders_for_sku_filters_to_the_requested_sku(self, mock_post):
        """An order matches on any of its SKUs, so other lines must be dropped."""
        mock_post.return_value = self._orders_resp(
            [
                {
                    "id": "gid://shopify/Order/1",
                    "name": "#1001",
                    "createdAt": "2026-07-01T10:00:00Z",
                    "cancelledAt": None,
                    "displayFinancialStatus": "PAID",
                    "displayFulfillmentStatus": "FULFILLED",
                    "_lines": [
                        {
                            "sku": "AIOPS-W1",
                            "name": "Widget",
                            "quantity": 2,
                            "currentQuantity": 2,
                            "unfulfilledQuantity": 0,
                        },
                        {
                            "sku": "OTHER-SKU",
                            "name": "Something else",
                            "quantity": 5,
                            "currentQuantity": 5,
                            "unfulfilledQuantity": 5,
                        },
                    ],
                }
            ]
        )
        rows = self._client().list_orders_for_sku("AIOPS-W1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order"], "#1001")
        self.assertEqual(rows[0]["financial_status"], "PAID")
        self.assertEqual([ln["sku"] for ln in rows[0]["line_items"]], ["AIOPS-W1"])
        self.assertEqual(rows[0]["line_items"][0]["qty_current"], 2)

    @patch(_PATH)
    def test_list_orders_for_sku_builds_the_search_query(self, mock_post):
        mock_post.return_value = self._orders_resp([])
        self._client().list_orders_for_sku("AIOPS-W1", limit=5, since_days=14)
        variables = mock_post.call_args.kwargs["json"]["variables"]
        self.assertIn('sku:"AIOPS-W1"', variables["q"])
        self.assertIn("created_at:>=", variables["q"])
        self.assertEqual(variables["first"], 5)

    @patch(_PATH)
    def test_list_orders_for_sku_without_date_bound(self, mock_post):
        mock_post.return_value = self._orders_resp([])
        self._client().list_orders_for_sku("AIOPS-W1", since_days=None)
        self.assertNotIn("created_at", mock_post.call_args.kwargs["json"]["variables"]["q"])

    @patch(_PATH)
    def test_list_orders_for_sku_drops_orders_without_a_matching_line(self, mock_post):
        # Shopify's search can return an order whose match came from elsewhere;
        # reporting it as a sale of this SKU would invent a missing sale.
        mock_post.return_value = self._orders_resp(
            [
                {
                    "id": "gid://shopify/Order/2",
                    "name": "#1002",
                    "createdAt": "2026-07-02T10:00:00Z",
                    "cancelledAt": None,
                    "displayFinancialStatus": "PAID",
                    "displayFulfillmentStatus": "FULFILLED",
                    "_lines": [{"sku": "OTHER-SKU", "name": "x", "quantity": 1}],
                }
            ]
        )
        self.assertEqual(self._client().list_orders_for_sku("AIOPS-W1"), [])

    def test_list_orders_for_sku_without_sku_is_empty(self):
        self.assertEqual(self._client().list_orders_for_sku(""), [])
