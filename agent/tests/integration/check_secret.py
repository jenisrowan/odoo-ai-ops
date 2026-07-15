"""Check which secret Shopify actually signed a captured delivery with.

The edge shim stores each delivery's exact bytes (`raw_b64`) plus its
`x-shopify-hmac-sha256`, so a candidate secret can be verified offline against a
REAL delivery - no new webhook required.

Usage (from the repo root):

    # test the secret currently in .env against every capture
    docker run --rm --env-file .env \
      -v "$PWD/agent/tests/integration:/t:ro" python:3.12-slim \
      python3 /t/check_secret.py

    # test a candidate (e.g. the custom app's API secret key)
    SHOPIFY_WEBHOOK_SECRET='shpss_...' python3 check_secret.py
"""

from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import pathlib
import sys

_base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__)
CAPTURES = pathlib.Path(_base) / "captures"
SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")

if not SECRET:
    raise SystemExit("set SHOPIFY_WEBHOOK_SECRET to the candidate secret")

files = sorted(glob.glob(str(CAPTURES / "*.json")))
if not files:
    raise SystemExit(f"no captures in {CAPTURES} - run the edge shim and trigger a webhook first")

real = fake = ok = 0
for f in files:
    d = json.loads(pathlib.Path(f).read_text())
    raw_b64 = d.get("raw_b64")
    if raw_b64 is None:
        print(f"skip (pre-raw_b64 capture): {pathlib.Path(f).name}")
        continue
    raw = base64.b64decode(raw_b64)
    provided = d["headers"].get("x-shopify-hmac-sha256", "")
    expected = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    match = hmac.compare_digest(expected, provided)
    from_shopify = "shopify" in d["headers"].get("user-agent", "").lower()
    real += from_shopify
    fake += not from_shopify
    ok += match and from_shopify
    print(
        f"{'REAL  ' if from_shopify else 'synth '} {d.get('topic'):<34} "
        f"{'MATCH' if match else 'no match':<9} {pathlib.Path(f).name}"
    )

print(f"\nreal Shopify deliveries: {real}, of which this secret verifies: {ok}")
if real and not ok:
    print("=> this secret is NOT what Shopify signs with (use the app's API secret key)")
elif ok:
    print("=> this secret is correct for real Shopify deliveries")
