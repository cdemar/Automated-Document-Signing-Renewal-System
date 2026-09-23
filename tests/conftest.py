"""Shared fixtures and fake data for the church forms test suite.

All tests share these fixtures via conftest.py so each test file stays
focused on assertions rather than setup boilerplate.
"""
import copy
import sys
import os
from datetime import date, timedelta

import pytest

# Make lambdas/ importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas"))

# ---------------------------------------------------------------------------
# Fake data constants
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret-abc123"

FAKE_CONFIG = {
    "ses_from": "noreply@example.org",
    "admin_email": "admin@example.org",
    "api_base_url": "https://api.example.com",
    "s3_bucket": "test-bucket",
    "retention_years": 7,
    "groups": {
        "background-checks": {
            "display_name": "Background Checks",
            "owner_email": "records@example.org",
            "owner_name": "Jane Smith",
            "owner_title": "Executive Pastor",
            "sheet_id": "sheet-abc-123",
            "forms": {
                "consent-form": {
                    "display_name": "CalVECHS Waiver Agreement",
                    "boldsign_template_id": "template-abc-123",
                    "retention_years": 10,
                    "countersign": False,
                }
            },
        }
    },
}


def _make_record(**overrides):
    """Return a base DynamoDB record dict, optionally overriding any field."""
    base = {
        "person_id": "person-uuid-123",
        "form_id": "consent-form",
        "name": "Alice Johnson",
        "email": "alice@example.com",
        "phone": "555-0100",
        "group": "background-checks",
        "form_type": "consent-form",
        "renewal_date": (date.today() + timedelta(days=30)).isoformat(),
        "signed": False,
        "opted_out": False,
        "owner_summary_sent": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return copy.deepcopy(FAKE_CONFIG)


@pytest.fixture
def record():
    return _make_record()


@pytest.fixture
def signed_record():
    return _make_record(signed=True, signed_at="2026-07-01T10:00:00+00:00")


@pytest.fixture
def opted_out_record():
    return _make_record(opted_out=True)


@pytest.fixture
def overdue_record():
    past = (date.today() - timedelta(days=5)).isoformat()
    return _make_record(renewal_date=past)
