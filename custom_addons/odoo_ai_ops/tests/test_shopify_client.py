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
        self.assertEqual(
            set(summary["created"]), {"orders/create", "orders/risk_assessment_changed"}
        )
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
        self.assertEqual(
            set(summary["unchanged"]), {"orders/create", "orders/risk_assessment_changed"}
        )
        # Only the list query is issued; no create/update writes.
        self.assertEqual(mock_post.call_count, 1)

    @patch(_PATH)
    def test_sync_webhooks_repoints_stale_url(self, mock_post):
        nodes = [{"id": "gid://shopify/WebhookSubscription/1", "topic": "ORDERS_CREATE", "endpoint": {"callbackUrl": "https://old/webhooks/shopify"}}]
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
