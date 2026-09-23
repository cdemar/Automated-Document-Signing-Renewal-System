"""Tests for reminder_checker email content and the Friday report.

What is tested
--------------
- _signer_email_body  : content of the plain-text signer emails
- _signer_email_html  : structure of the HTML signer emails
- _friday_report_text : plain-text weekly owner report
- _friday_report_html : HTML weekly owner report (stat tiles, row colors)

What is NOT tested here
-----------------------
- lambda_handler itself (requires DynamoDB + SES mocks for the full flow)
- _ensure_document (calls BoldSign API)
- _send_email (calls SES)
"""
from datetime import date, timedelta

import pytest
import optout_token
import reminder_checker


# ---------------------------------------------------------------------------
# Setup: bypass AWS Secrets Manager for the optout_token module
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def inject_optout_secret(monkeypatch):
    monkeypatch.setattr(optout_token, "_secret", "test-secret-key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_opt_url(config, record):
    return optout_token.optout_url(
        config["api_base_url"], record["person_id"], record["form_id"]
    )


def _past(days=1):
    return (date.today() - timedelta(days=days)).isoformat()


def _future(days=30):
    return (date.today() + timedelta(days=days)).isoformat()


def _report_record(**overrides):
    base = {
        "name": "Test Person",
        "email": "test@example.com",
        "renewal_date": _future(),
        "signed": False,
        "opted_out": False,
        "owner_summary_sent": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _signer_email_body — plain-text signer emails
# ---------------------------------------------------------------------------

class TestSignerEmailBody:
    def test_initial_email_opens_with_grace(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert "Grace and peace to you" in body

    def test_final_email_has_reminder_header(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=True)
        assert "REMINDER TO COMPLETE" in body

    def test_email_contains_sign_link(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link/abc", final=False)
        assert "https://sign.link/abc" in body

    def test_email_contains_deadline(self, config, record):
        record["renewal_date"] = "2026-08-15"
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert "2026-08-15" in body

    def test_email_contains_opt_out_link(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert "opt out" in body.lower()

    def test_email_signed_off_with_owner_name(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert "Jane Smith" in body

    def test_email_signed_off_with_owner_title(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert "Executive Pastor" in body

    def test_email_addressed_to_signer(self, config, record):
        body = reminder_checker._signer_email_body(config, record, "https://sign.link", final=False)
        assert record["name"] in body


# ---------------------------------------------------------------------------
# _signer_email_html — HTML signer emails
# ---------------------------------------------------------------------------

class TestSignerEmailHtml:
    def test_html_is_valid_structure(self, config, record):
        opt_url = _fake_opt_url(config, record)
        html = reminder_checker._signer_email_html(config, record, "https://sign.link", opt_url, final=False)
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_html_contains_sign_now_button(self, config, record):
        opt_url = _fake_opt_url(config, record)
        html = reminder_checker._signer_email_html(config, record, "https://sign.link", opt_url, final=False)
        assert "Sign Now" in html

    def test_html_sign_button_links_to_sign_url(self, config, record):
        opt_url = _fake_opt_url(config, record)
        html = reminder_checker._signer_email_html(
            config, record, "https://sign.link/xyz", opt_url, final=False
        )
        assert 'href="https://sign.link/xyz"' in html

    def test_html_contains_opt_out_link(self, config, record):
        opt_url = _fake_opt_url(config, record)
        html = reminder_checker._signer_email_html(config, record, "https://sign.link", opt_url, final=False)
        assert opt_url in html

    @pytest.mark.parametrize("final", [True, False])
    def test_html_always_contains_signer_name(self, config, record, final):
        opt_url = _fake_opt_url(config, record)
        html = reminder_checker._signer_email_html(config, record, "https://sign.link", opt_url, final=final)
        assert record["name"] in html


# ---------------------------------------------------------------------------
# _friday_report_text — plain-text weekly owner report
# ---------------------------------------------------------------------------

class TestFridayReportText:
    def _report(self, records):
        return reminder_checker._friday_report_text(
            "background-checks", records, date.today().isoformat()
        )

    def test_shows_signed_count(self):
        records = [_report_record(signed=True)]
        assert "1 signed" in self._report(records)

    def test_shows_opted_out_count(self):
        records = [_report_record(opted_out=True)]
        assert "1 opted out" in self._report(records)

    def test_first_time_overdue_labeled_new_this_week(self):
        records = [_report_record(renewal_date=_past(), owner_summary_sent=False)]
        assert "NEW THIS WEEK" in self._report(records)

    def test_repeat_overdue_labeled_still_overdue(self):
        records = [_report_record(renewal_date=_past(8), owner_summary_sent=True)]
        assert "STILL OVERDUE" in self._report(records)

    def test_all_caught_up_when_no_overdue(self):
        records = [_report_record(signed=True)]
        assert "all caught up" in self._report(records).lower()

    def test_pending_records_are_listed(self):
        records = [_report_record(renewal_date=_future(7))]
        assert "PENDING" in self._report(records)

    def test_group_name_in_output(self):
        assert "background-checks" in self._report([])


# ---------------------------------------------------------------------------
# _friday_report_html — HTML weekly owner report
# ---------------------------------------------------------------------------

class TestFridayReportHtml:
    def _report(self, records):
        return reminder_checker._friday_report_html(
            "background-checks", records, date.today().isoformat()
        )

    def test_html_has_valid_structure(self):
        html = self._report([])
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_all_four_stat_tiles_present(self):
        html = self._report([])
        for label in ("Signed", "Opted Out", "Overdue", "Pending"):
            assert label in html

    def test_yellow_row_for_first_time_overdue(self):
        """🟡 First-time overdue → yellow background #FEF3C7."""
        records = [_report_record(renewal_date=_past(), owner_summary_sent=False)]
        assert "#FEF3C7" in self._report(records)

    def test_red_row_for_repeat_overdue(self):
        """🔴 Repeat overdue → red background #FEE2E2."""
        records = [_report_record(renewal_date=_past(8), owner_summary_sent=True)]
        assert "#FEE2E2" in self._report(records)

    def test_green_row_for_signed(self):
        """✅ Signed → green background #F0FDF4."""
        records = [_report_record(signed=True)]
        assert "#F0FDF4" in self._report(records)

    def test_gray_row_for_opted_out(self):
        """⛔ Opted out → gray background #F9FAFB."""
        records = [_report_record(opted_out=True)]
        assert "#F9FAFB" in self._report(records)

    def test_blue_row_for_pending(self):
        """🕐 Not yet due → blue background #EFF6FF."""
        records = [_report_record(renewal_date=_future(10))]
        assert "#EFF6FF" in self._report(records)

    def test_caught_up_message_when_no_records(self):
        """Empty record list → no sections → 'All caught up' fallback message."""
        assert "All caught up" in self._report([])

    def test_group_name_appears_in_header(self):
        assert "background-checks" in self._report([])

    def test_today_date_appears_in_header(self):
        assert date.today().isoformat() in self._report([])
