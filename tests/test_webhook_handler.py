"""Tests for the BoldSign webhook handler.

Strategy
--------
- Pure helpers (_safe_name, _retention_years) are tested directly — no mocking.
- _verify_signature is tested by building real HMAC signatures in the test
  so we exercise the actual crypto path, not a stub.
- lambda_handler entry-point tests only reach the early-return branches
  (empty body, no signature, bad signature, ignored event, missing documentId).
  Each of those returns BEFORE _load_config is called, so no S3 mock is needed.
"""
import hashlib
import hmac as hmac_lib
import json
import time
from unittest.mock import MagicMock, patch

import pytest
import webhook_handler


WEBHOOK_SECRET = "test-webhook-secret-abc123"


# ---------------------------------------------------------------------------
# Setup: inject the cached secret so _get_webhook_secret() never calls AWS
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_secret(monkeypatch):
    monkeypatch.setattr(webhook_handler, "_webhook_secret", WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_signature(body: str, secret: str = WEBHOOK_SECRET, age_seconds: int = 0) -> str:
    """Build a valid X-BoldSign-Signature header value."""
    ts = int(time.time()) - age_seconds
    payload = f"{ts}.{body}".encode("utf-8")
    sig = hmac_lib.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"t={ts},s0={sig}"


def make_event(body: str, signature: str = None) -> dict:
    headers = {}
    if signature is not None:
        headers["x-boldsign-signature"] = signature
    return {"body": body, "headers": headers, "isBase64Encoded": False}


# ---------------------------------------------------------------------------
# _safe_name
# ---------------------------------------------------------------------------

class TestSafeName:
    @pytest.mark.parametrize("raw,expected", [
        ("Alice Johnson",  "Alice_Johnson"),
        ("O'Brien",        "O_Brien"),
        ("John  Doe",      "John_Doe"),      # multiple spaces → one underscore
        ("valid-name_123", "valid-name_123"),# hyphens and underscores are kept
        ("_leading",       "leading"),       # leading underscore stripped
        ("trailing_",      "trailing"),      # trailing underscore stripped
        ("!!!",            "unknown"),       # all special chars → empty → "unknown"
        ("",               "unknown"),
    ])
    def test_sanitization(self, raw, expected):
        assert webhook_handler._safe_name(raw) == expected


# ---------------------------------------------------------------------------
# _verify_signature
# ---------------------------------------------------------------------------

class TestVerifySignature:
    def test_valid_signature_passes(self):
        body = '{"event":{"eventType":"Completed"}}'
        header = build_signature(body)
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": header}, body
        ) is True

    def test_missing_header_fails(self):
        assert webhook_handler._verify_signature({}, "any-body") is False

    def test_wrong_secret_fails(self):
        body = '{"event":{"eventType":"Completed"}}'
        header = build_signature(body, secret="wrong-secret")
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": header}, body
        ) is False

    def test_tampered_body_fails(self):
        body = '{"event":{"eventType":"Completed"}}'
        header = build_signature(body)
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": header}, "tampered-body"
        ) is False

    def test_expired_timestamp_fails(self):
        """Timestamps older than 300 s must be rejected."""
        body = "payload"
        header = build_signature(body, age_seconds=400)
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": header}, body
        ) is False

    def test_fresh_timestamp_near_limit_passes(self):
        """A timestamp just inside the 300 s window must be accepted."""
        body = "payload"
        header = build_signature(body, age_seconds=290)
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": header}, body
        ) is True

    @pytest.mark.parametrize("bad_header", [
        "garbage",
        "t=abc,s0=nothex",  # non-numeric timestamp
        "s0=onlysig",       # missing timestamp
        "t=12345",          # missing signature
    ])
    def test_malformed_header_fails(self, bad_header):
        assert webhook_handler._verify_signature(
            {"x-boldsign-signature": bad_header}, "body"
        ) is False


# ---------------------------------------------------------------------------
# _retention_years
# ---------------------------------------------------------------------------

class TestRetentionYears:
    def test_per_form_value_wins(self, config):
        record = {"group": "background-checks", "form_type": "consent-form"}
        assert webhook_handler._retention_years(config, record) == 10

    def test_falls_back_to_global_config(self, config):
        record = {"group": "background-checks", "form_type": "unknown-form"}
        assert webhook_handler._retention_years(config, record) == 7

    def test_falls_back_to_hardcoded_default(self):
        record = {"group": "no-group", "form_type": "no-form"}
        assert (
            webhook_handler._retention_years({}, record)
            == webhook_handler.DEFAULT_RETENTION_YEARS
        )

    def test_missing_group_uses_global(self, config):
        record = {"group": "nonexistent-group", "form_type": "consent-form"}
        assert webhook_handler._retention_years(config, record) == 7


# ---------------------------------------------------------------------------
# lambda_handler — early-return entry points (no AWS calls needed)
# ---------------------------------------------------------------------------

class TestLambdaHandlerEntryPoints:
    def test_empty_body_returns_200(self):
        """BoldSign health-check ping: empty body must get a 200 immediately."""
        result = webhook_handler.lambda_handler(
            {"body": "", "headers": {}, "isBase64Encoded": False}, None
        )
        assert result["statusCode"] == 200

    def test_no_signature_header_returns_200(self):
        """Setup verification ping arrives without a signature — must accept."""
        result = webhook_handler.lambda_handler(
            make_event('{"something":"here"}'), None
        )
        assert result["statusCode"] == 200

    def test_invalid_signature_returns_401(self):
        body = '{"event":{"eventType":"Completed"}}'
        header = build_signature(body, secret="wrong-secret")
        result = webhook_handler.lambda_handler(make_event(body, header), None)
        assert result["statusCode"] == 401

    @pytest.mark.parametrize("event_type", [
        "SomethingElse",
        "Viewed",
        "Declined",
        "",
    ])
    def test_unrecognized_events_return_200_ignored(self, event_type):
        body = json.dumps({"event": {"eventType": event_type}})
        header = build_signature(body)
        result = webhook_handler.lambda_handler(make_event(body, header), None)
        assert result["statusCode"] == 200
        assert "ignored" in result["body"]

    def test_missing_document_id_returns_400(self):
        """Completed event with no documentId in the payload is a bad request."""
        body = json.dumps({
            "event": {"eventType": "Completed"},
            "document": {},
        })
        header = build_signature(body)
        with patch.object(webhook_handler, "_load_config", return_value={}):
            result = webhook_handler.lambda_handler(make_event(body, header), None)
        assert result["statusCode"] == 400

    def test_no_matching_dynamo_record_returns_200(self):
        """If no DynamoDB record matches, return 200 so BoldSign doesn't retry."""
        doc_id = "doc-abc-123"
        body = json.dumps({
            "event": {"eventType": "Completed"},
            "document": {"documentId": doc_id},
        })
        header = build_signature(body)
        mock_table = MagicMock()
        mock_table.get_item.return_value = {}
        mock_table.scan.return_value = {"Items": []}
        with (
            patch.object(webhook_handler, "_load_config", return_value={}),
            patch.object(webhook_handler, "dynamodb") as mock_dynamo,
        ):
            mock_dynamo.Table.return_value = mock_table
            result = webhook_handler.lambda_handler(make_event(body, header), None)
        assert result["statusCode"] == 200
        assert "no matching record" in result["body"]
