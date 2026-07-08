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
