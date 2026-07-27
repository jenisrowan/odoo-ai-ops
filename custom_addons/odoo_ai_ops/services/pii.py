# -*- coding: utf-8 -*-
"""Privacy redaction for anything Odoo sends to the LLM (Anthropic Claude).

Customer and staff data is stored in full inside Odoo, but the fraud and
reconciliation workflows send order/inventory context out to Anthropic for
analysis. This module is the single place that decides what leaves the box.

The rule is simple: **if GDPR treats a field as personal data, it does not go to
Anthropic.** Everything else stays. The goal is to lose no fraud signal - so
rather than blanking a field outright we derive the signal that lives in it and
send that instead:

* the email address becomes just its domain (``@gmail.com`` vs ``@protonmail.com``
  is a genuine signal; the mailbox name is not);
* the phone number becomes just its international dialling code (a French address
  paired with a Hungarian ``+36`` is a red flag; the digits are not);
* the two addresses become their country / province / city / postcode plus an
  ``addresses_match`` flag worked out from the full addresses (street included)
  before the street is dropped;
* the browser IP and session hash are dropped outright - an IP address is
  personal data under GDPR, and Shopify's own risk facts already carry the
  IP/proxy/geolocation checks, so no signal is lost.

Staff and customer *names* that appear in the reconciliation evidence are
pseudonymised instead of dropped (the analysis needs to say *who* forced a stock
count): each name becomes a stable ``Employee <id>`` / ``Customer <id>`` token
that Anthropic cannot resolve to a person. Odoo keeps the id→name mapping and
swaps the real names back in (:func:`rehydrate`) for the Slack card and the task
record, so a manager sees no difference.

Deliberately free of Odoo imports so it can be unit-tested in isolation, exactly
like ``shopify_client.py``.
"""

import re

# --- E.164 country calling codes -------------------------------------------
# Used to pull the dialling code off a phone number (longest-prefix match). This
# is the whole of the number we keep: enough to spot a phone whose country does
# not match the address, none of the identifying subscriber digits. A number in
# national format (no ``+`` / ``00``) carries no country and yields ``None``.
_CALLING_CODES = {
    "1",
    "7",
    "20",
    "27",
    "30",
    "31",
    "32",
    "33",
    "34",
    "36",
    "39",
    "40",
    "41",
    "43",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",
    "51",
    "52",
    "53",
    "54",
    "55",
    "56",
    "57",
    "58",
    "60",
    "61",
    "62",
    "63",
    "64",
    "65",
    "66",
    "81",
    "82",
    "84",
    "86",
    "90",
    "91",
    "92",
    "93",
    "94",
    "95",
    "98",
    "211",
    "212",
    "213",
    "216",
    "218",
    "220",
    "221",
    "222",
    "223",
    "224",
    "225",
    "226",
    "227",
    "228",
    "229",
    "230",
    "231",
    "232",
    "233",
    "234",
    "235",
    "236",
    "237",
    "238",
    "239",
    "240",
    "241",
    "242",
    "243",
    "244",
    "245",
    "246",
    "248",
    "249",
    "250",
    "251",
    "252",
    "253",
    "254",
    "255",
    "256",
    "257",
    "258",
    "260",
    "261",
    "262",
    "263",
    "264",
    "265",
    "266",
    "267",
    "268",
    "269",
    "290",
    "291",
    "297",
    "298",
    "299",
    "350",
    "351",
    "352",
    "353",
    "354",
    "355",
    "356",
    "357",
    "358",
    "359",
    "370",
    "371",
    "372",
    "373",
    "374",
    "375",
    "376",
    "377",
    "378",
    "380",
    "381",
    "382",
    "383",
    "385",
    "386",
    "387",
    "389",
    "420",
    "421",
    "423",
    "500",
    "501",
    "502",
    "503",
    "504",
    "505",
    "506",
    "507",
    "508",
    "509",
    "590",
    "591",
    "592",
    "593",
    "594",
    "595",
    "596",
    "597",
    "598",
    "599",
    "670",
    "672",
    "673",
    "674",
    "675",
    "676",
    "677",
    "678",
    "679",
    "680",
    "681",
    "682",
    "683",
    "685",
    "686",
    "687",
    "688",
    "689",
    "690",
    "691",
    "692",
    "850",
    "852",
    "853",
    "855",
    "856",
    "880",
    "886",
    "960",
    "961",
    "962",
    "963",
    "964",
    "965",
    "966",
    "967",
    "968",
    "970",
    "971",
    "972",
    "973",
    "974",
    "975",
    "976",
    "977",
    "992",
    "993",
    "994",
    "995",
    "996",
    "998",
}

# client_details keys that are NOT personal data and may pass through. Whitelist,
# not blacklist: a scrubber must fail closed, so an unrecognised (possibly
# identifying) future field is dropped rather than leaked. browser_ip and
# session_hash are the identifying ones and are absent from this list.
_CLIENT_DETAIL_KEEP = ("accept_language", "browser_height", "browser_width", "user_agent")

# Address keys that describe an area, not a person. Street lines, names, phone
# and lat/long are personal data and are absent here.
_ADDRESS_KEEP = ("country", "country_code", "province", "province_code", "city", "zip")

# The subset of address fields used to decide whether two addresses are "the
# same". Includes the street so the match reflects the doorstep, even though the
# street itself never leaves Odoo.
_ADDRESS_MATCH_KEYS = ("address1", "address2", "city", "zip", "country_code")

# --- Name pseudonymisation -------------------------------------------------
# Tokens are ``<kind> <db-id>``: stable (same person → same token across every
# call), correlatable by the model, but meaningless to anyone without Odoo's
# database. rehydrate() reverses them for display.
EMPLOYEE = "Employee"
CUSTOMER = "Customer"
_TOKEN_RE = re.compile(r"\b(%s|%s) (\d+)\b" % (EMPLOYEE, CUSTOMER))


def email_domain(email):
    """Return the lower-cased domain of an email address, or ``None``.

    ``"Bob@Example.COM"`` -> ``"example.com"``. The mailbox (local) part, which
    identifies the person, is discarded.
    """
    if not email or "@" not in str(email):
        return None
    domain = str(email).rsplit("@", 1)[1].strip().lower()
    return domain or None


def phone_dialling_code(phone):
    """Return the international dialling code of a phone number (e.g. ``"+36"``).

    Returns ``None`` for a number in national format (no ``+`` or ``00``
    prefix), because such a number carries no country information at all - there
    is nothing to compare against the address, and nothing identifying to keep.
    """
    if not phone:
        return None
    raw = str(phone).strip()
    if not (raw.startswith("+") or raw.startswith("00")):
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    # Longest-prefix match: codes are 1–3 digits and some are prefixes of
    # others, so try the longest first. Require at least one subscriber digit
    # after the code so a bare code is not mistaken for a whole number.
    for length in (3, 2, 1):
        prefix = digits[:length]
        if len(digits) > length and prefix in _CALLING_CODES:
            return "+" + prefix
    return None


def redact_address(addr):
    """Keep only the area-level fields of a Shopify address; drop the rest.

    Country, province, city and postcode stay (none identifies a person on its
    own once the street is gone); name, street lines, phone and coordinates go.
    """
    if not isinstance(addr, dict):
        return {}
    return {k: addr.get(k) for k in _ADDRESS_KEEP if addr.get(k) is not None}


def addresses_match(billing, shipping):
    """Whether billing and shipping are the same address, street included.

    Computed here, while the full addresses are still in hand, so the doorstep
    match survives as a boolean without the doorstep leaving Odoo. Returns
    ``None`` when either address is missing (nothing to compare).
    """
    if not isinstance(billing, dict) or not isinstance(shipping, dict):
        return None
    if not billing or not shipping:
        return None

    def _norm(addr):
        return tuple(str(addr.get(k) or "").strip().lower() for k in _ADDRESS_MATCH_KEYS)

    return _norm(billing) == _norm(shipping)


def redact_client_details(client_details):
    """Keep the non-identifying browser fields; drop the IP and session hash."""
    if not isinstance(client_details, dict):
        return {}
    return {k: client_details.get(k) for k in _CLIENT_DETAIL_KEEP if client_details.get(k) is not None}


def redact_fraud_order(order):
    """Turn a full Shopify ``orders/create`` order into a privacy-safe context.

    The returned dict carries the fraud signal the LLM needs (email domain,
    phone dialling code, address regions, address-match flag, browser fingerprint
    minus the IP, payment gateways, account age, line items) and none of the
    personal data (names, email address, phone digits, street, coordinates, IP,
    session hash). See the module docstring for the field-by-field rationale.
    """
    order = order if isinstance(order, dict) else {}
    customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
    billing = order.get("billing_address") if isinstance(order.get("billing_address"), dict) else {}
    shipping = order.get("shipping_address") if isinstance(order.get("shipping_address"), dict) else {}

    email = order.get("email") or order.get("contact_email") or customer.get("email")
    phone = order.get("phone") or customer.get("phone") or billing.get("phone") or shipping.get("phone")

    details = {}
    if order.get("created_at") is not None:
        details["created_at"] = order.get("created_at")

    domain = email_domain(email)
    if domain:
        details["email_domain"] = domain

    code = phone_dialling_code(phone)
    if code:
        details["phone_dialling_code"] = code

    redacted_billing = redact_address(billing)
    if redacted_billing:
        details["billing_address"] = redacted_billing
    redacted_shipping = redact_address(shipping)
    if redacted_shipping:
        details["shipping_address"] = redacted_shipping

    match = addresses_match(billing, shipping)
    if match is not None:
        details["addresses_match"] = match

    client_details = redact_client_details(order.get("client_details"))
    if client_details:
        details["client_details"] = client_details

    gateways = order.get("payment_gateway_names")
    if gateways:
        details["payment_gateway_names"] = gateways

    # Account facts that describe the account, not the person. All non-personal:
    # a sign-up date, order counts and a verification flag identify no one.
    account = {}
    for src, dst in (
        ("created_at", "account_created_at"),
        ("state", "state"),
        ("orders_count", "orders_count"),
        ("total_spent", "total_spent"),
        ("verified_email", "verified_email"),
    ):
        if customer.get(src) is not None:
            account[dst] = customer.get(src)
    if account:
        details["customer"] = account

    # Line items carry no personal data.
    details["line_items"] = [
        {k: item.get(k) for k in ("title", "sku", "quantity", "price")}
        for item in (order.get("line_items") or [])
        if isinstance(item, dict)
    ]
    return details


def pseudonymise(kind, record_id):
    """Return the stable token for a person, e.g. ``pseudonymise(EMPLOYEE, 42)``."""
    return "%s %s" % (kind, record_id)


def rehydrate(text, resolver):
    """Swap ``Employee <id>`` / ``Customer <id>`` tokens back to real names.

    ``resolver(kind, record_id)`` returns the display name for a token, or a
    falsy value to leave the token in place (e.g. the record no longer exists).
    """
    if not text:
        return text

    def _replace(match):
        kind, record_id = match.group(1), int(match.group(2))
        return resolver(kind, record_id) or match.group(0)

    return _TOKEN_RE.sub(_replace, str(text))
