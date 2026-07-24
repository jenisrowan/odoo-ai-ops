"""Test setup for the webhook authorizer Lambda.

Environment is configured and the handler package is put on ``sys.path`` at
import time (before the handler module - which creates boto3 clients and reads
env at import - is loaded). Fully offline; no AWS is contacted.
"""

import os
import pathlib
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "webhook_authorizer")
)

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-south-1")
os.environ.setdefault("SQS_QUEUE_URL", "https://sqs.test.local/queue")
# _secret() falls back to KEY.upper() env vars when no INTEGRATION_SECRET_ARN.
#
# Assigned, not setdefault: test_handler.py signs its fixtures with these exact
# dummy values, so a real secret inherited from the environment (running under
# `docker run --env-file .env`, say) would make every valid-signature test fail
# with a 401 that looks like a handler bug. The unit tests must be hermetic.
# test_captured_deliveries.py deliberately wants the real secret and sources it
# from the repo .env itself.
os.environ["SHOPIFY_WEBHOOK_SECRET"] = "shpsecret"
os.environ["SLACK_SIGNING_SECRET"] = "slacksecret"
