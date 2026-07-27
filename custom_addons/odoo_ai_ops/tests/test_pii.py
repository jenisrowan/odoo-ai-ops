# -*- coding: utf-8 -*-
"""Tests for privacy redaction of everything Odoo sends to the LLM.

Two layers:

* pure-function tests for ``services/pii.py`` (no Odoo needed);
* an end-to-end check that ``ai.ops.task._fraud_order_context`` - the thing that
  actually builds the payload sent to the agent/Anthropic - emits no personal
  data, plus the reconciliation name round-trip through ``ai_ops_rehydrate``.

The end-to-end fraud check is the one that matters: it asserts against the real
producer, so if anyone later widens what Odoo forwards, this breaks.
"""

import json
from unittest.mock import patch

from odoo.addons.odoo_ai_ops.services import pii
from odoo.tests import TransactionCase, tagged

# A deliberately PII-rich Shopify orders/create order, so the assertions below
# have something real to fail on if a field leaks.
_RAW_ORDER = {
    "id": "8001",
    "name": "#8001",
    "created_at": "2026-07-01T10:00:00-04:00",
    "email": "jane.doe@protonmail.com",
    "contact_email": "jane.doe@protonmail.com",
    "phone": "+36 1 234 5678",
    "payment_gateway_names": ["shopify_payments"],
    "billing_address": {
        "first_name": "Jane",
        "last_name": "Doe",
        "name": "Jane Doe",
        "address1": "221B Baker Street",
        "address2": "Flat 2",
        "phone": "+36 1 234 5678",
        "city": "London",
        "province": "England",
        "province_code": "ENG",
        "zip": "NW1 6XE",
        "country": "United Kingdom",
        "country_code": "GB",
        "latitude": 51.5237,
        "longitude": -0.1585,
        "company": "Doe Ltd",
    },
    "shipping_address": {
        "first_name": "Marc",
        "last_name": "Blanc",
        "name": "Marc Blanc",
        "address1": "10 Rue de Rivoli",
        "phone": "+33 1 42 00 00 00",
        "city": "Paris",
        "province": "Île-de-France",
        "zip": "75001",
        "country": "France",
        "country_code": "FR",
        "latitude": 48.8566,
        "longitude": 2.3522,
    },
    "client_details": {
        "accept_language": "en-GB",
        "browser_height": 900,
        "browser_width": 1440,
        "browser_ip": "203.0.113.42",
        "session_hash": "deadbeefcafef00d",
        "user_agent": "Mozilla/5.0",
    },
    "customer": {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane.doe@protonmail.com",
        "phone": "+36 1 234 5678",
        "created_at": "2026-06-30T09:00:00-04:00",
        "state": "enabled",
        "orders_count": 0,
        "verified_email": True,
    },
    "line_items": [
        {"title": "Gold Bar", "sku": "AU-1KG", "quantity": 3, "price": "60000.00"},
    ],
}

# Things that must never appear anywhere in the redacted output.
_FORBIDDEN = [
    "Jane",
    "Doe",
    "Marc",
    "Blanc",  # names
    "jane.doe",  # email local part
    "221B",
    "Baker",
    "Rivoli",  # streets
    "203.0.113.42",  # IP
    "deadbeefcafef00d",  # session hash
    "51.5237",
    "48.8566",  # coordinates
    "234 5678",
    "1234567",  # phone subscriber digits
]


@tagged("post_install", "-at_install", "ai_ops")
class TestPiiHelpers(TransactionCase):
    """Pure-function behaviour of services/pii.py."""

    def test_email_domain_keeps_only_the_domain(self):
        self.assertEqual(pii.email_domain("Bob@Example.COM"), "example.com")
        self.assertEqual(pii.email_domain("jane.doe@protonmail.com"), "protonmail.com")
        self.assertIsNone(pii.email_domain("not-an-email"))
        self.assertIsNone(pii.email_domain(None))

    def test_phone_dialling_code_extracts_country_code(self):
        self.assertEqual(pii.phone_dialling_code("+36 1 234 5678"), "+36")
        self.assertEqual(pii.phone_dialling_code("+33142000000"), "+33")
        self.assertEqual(pii.phone_dialling_code("+1 (415) 555-0100"), "+1")
        self.assertEqual(pii.phone_dialling_code("+212 5 22 00 00 00"), "+212")
        self.assertEqual(pii.phone_dialling_code("0036 1 234 5678"), "+36")

    def test_phone_dialling_code_is_none_without_country_info(self):
        # National format carries no country -> no signal, nothing to keep.
        self.assertIsNone(pii.phone_dialling_code("01 234 5678"))
        self.assertIsNone(pii.phone_dialling_code(""))
        self.assertIsNone(pii.phone_dialling_code(None))

    def test_redact_address_keeps_only_regions(self):
        out = pii.redact_address(_RAW_ORDER["billing_address"])
        self.assertEqual(out.get("city"), "London")
        self.assertEqual(out.get("zip"), "NW1 6XE")
        self.assertEqual(out.get("country_code"), "GB")
        for gone in (
            "first_name",
            "last_name",
            "name",
            "address1",
            "address2",
            "phone",
            "latitude",
            "longitude",
            "company",
        ):
            self.assertNotIn(gone, out)

    def test_addresses_match_uses_the_street(self):
        billing = {"address1": "1 A St", "city": "X", "zip": "1", "country_code": "GB"}
        self.assertTrue(pii.addresses_match(billing, dict(billing)))
        self.assertFalse(pii.addresses_match(billing, dict(billing, address1="2 B St")))
        # Missing address -> nothing to compare.
        self.assertIsNone(pii.addresses_match(billing, {}))

    def test_client_details_drops_ip_and_session_hash(self):
        out = pii.redact_client_details(_RAW_ORDER["client_details"])
        self.assertIn("user_agent", out)
        self.assertIn("accept_language", out)
        self.assertNotIn("browser_ip", out)
        self.assertNotIn("session_hash", out)

    def test_redact_fraud_order_carries_signal_not_pii(self):
        out = pii.redact_fraud_order(_RAW_ORDER)
        # Signal preserved.
        self.assertEqual(out["email_domain"], "protonmail.com")
        self.assertEqual(out["phone_dialling_code"], "+36")
        self.assertEqual(out["billing_address"]["country_code"], "GB")
        self.assertEqual(out["shipping_address"]["country_code"], "FR")
        self.assertFalse(out["addresses_match"])  # London vs Paris
        self.assertEqual(out["line_items"][0]["sku"], "AU-1KG")
        # No personal data anywhere in the serialized result.
        blob = json.dumps(out)
        for needle in _FORBIDDEN:
            self.assertNotIn(needle, blob, "leaked %r in redacted order" % needle)

    def test_rehydrate_restores_names_via_resolver(self):
        text = "Employee 42 forced the count; goods went to Customer 7."
        resolved = pii.rehydrate(
            text, lambda kind, rid: {("Employee", 42): "Alice", ("Customer", 7): "Bob Co"}.get((kind, rid))
        )
        self.assertEqual(resolved, "Alice forced the count; goods went to Bob Co.")
        # An unresolvable token is left as-is, never blanked.
        self.assertEqual(pii.rehydrate("Employee 99", lambda k, r: None), "Employee 99")


@tagged("post_install", "-at_install", "ai_ops")
class TestFraudContextRedaction(TransactionCase):
    """The real producer must emit no personal data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Task = cls.env["ai.ops.task"]
        cls.SaleOrder = cls.env["sale.order"]

    def _task_with_raw_order(self):
        partner = self.env["res.partner"].create({"name": "Buyer"})
        order = self.SaleOrder.create(
            {
                "partner_id": partner.id,
                "shopify_order_id": "8001",
                "shopify_order_name": "#8001",
                "shopify_raw_payload": json.dumps(_RAW_ORDER),
            }
        )
        return self.Task.create(
            {
                "task_type": "fraud",
                "risk_level": "high",
                "shopify_order_id": "8001",
                "shopify_order_name": "#8001",
                "sale_order_id": order.id,
                "state": "queued",
            }
        )

    def test_fraud_context_has_no_pii(self):
        task = self._task_with_raw_order()
        # Don't reach out to Shopify for the risk enrichment in a unit test.
        with patch.object(type(task), "_shopify_client") as mock_client:
            mock_client.return_value.get_order_risk_context.return_value = {}
            context = task._fraud_order_context()

        order = context["order"]
        self.assertEqual(order["email_domain"], "protonmail.com")
        self.assertEqual(order["phone_dialling_code"], "+36")
        self.assertFalse(order["addresses_match"])

        blob = json.dumps(context)
        for needle in _FORBIDDEN:
            self.assertNotIn(needle, blob, "leaked %r in fraud context" % needle)

    def test_rehydrate_round_trips_a_real_user(self):
        Inventory = self.env["ai.ops.inventory"]
        user = self.env.user
        token = pii.pseudonymise(pii.EMPLOYEE, user.id)
        text = "%s forced the count." % token
        restored = Inventory.ai_ops_rehydrate(text)
        self.assertIn(user.display_name, restored)
        self.assertNotIn(token, restored)
