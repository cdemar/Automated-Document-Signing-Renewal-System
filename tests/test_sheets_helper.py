"""Tests for the shared Google Sheets utilities.

Only the pure-logic functions are tested here:
  - _hex_to_rgb      (color conversion)
  - status_for_record (DynamoDB record → dashboard status string)
  - COLUMNS / COL_INDEX (schema integrity)

Functions that call the Google Sheets API (read_people_rows, overwrite_people_rows,
update_cell, append_audit) require a live service account and are excluded from
the unit-test suite.
"""
from datetime import date, timedelta

import pytest
from sheets_helper import (
    COL_INDEX,
    COLUMNS,
    STATUS_DECLINED,
    STATUS_OVERDUE,
    STATUS_PENDING,
    STATUS_RENEWED,
    _hex_to_rgb,
    status_for_record,
)


# ---------------------------------------------------------------------------
# _hex_to_rgb
# ---------------------------------------------------------------------------

class TestHexToRgb:
    @pytest.mark.parametrize("hex_color,expected", [
        ("#ffffff", {"red": 1.0,  "green": 1.0,  "blue": 1.0}),
        ("#000000", {"red": 0.0,  "green": 0.0,  "blue": 0.0}),
        ("#ff0000", {"red": 1.0,  "green": 0.0,  "blue": 0.0}),
        ("#00ff00", {"red": 0.0,  "green": 1.0,  "blue": 0.0}),
        ("#0000ff", {"red": 0.0,  "green": 0.0,  "blue": 1.0}),
    ])
    def test_known_colors(self, hex_color, expected):
        result = _hex_to_rgb(hex_color)
        assert result == pytest.approx(expected, abs=0.01)

    def test_returns_all_three_channels(self):
        assert set(_hex_to_rgb("#aabbcc").keys()) == {"red", "green", "blue"}

    def test_all_values_in_0_to_1_range(self):
        for v in _hex_to_rgb("#7f8fa6").values():
            assert 0.0 <= v <= 1.0

    def test_strips_hash_prefix(self):
        """Function must handle the leading # without errors."""
        result = _hex_to_rgb("#80ff40")
        assert isinstance(result["red"], float)


# ---------------------------------------------------------------------------
# status_for_record
# ---------------------------------------------------------------------------

class TestStatusForRecord:
    def test_signed_record_returns_renewed(self, signed_record):
        assert status_for_record(signed_record, date.today().isoformat()) == STATUS_RENEWED

    def test_opted_out_record_returns_declined(self, opted_out_record):
        assert status_for_record(opted_out_record, date.today().isoformat()) == STATUS_DECLINED

    def test_signed_beats_opted_out(self):
        """A record that is both signed and opted_out should show as Renewed."""
        record = {"signed": True, "opted_out": True, "renewal_date": "2099-01-01"}
        assert status_for_record(record, date.today().isoformat()) == STATUS_RENEWED

    def test_past_renewal_date_returns_overdue(self, overdue_record):
        assert status_for_record(overdue_record, date.today().isoformat()) == STATUS_OVERDUE

    def test_future_renewal_date_returns_pending(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        record = {"signed": False, "opted_out": False, "renewal_date": future}
        assert status_for_record(record, date.today().isoformat()) == STATUS_PENDING

    def test_missing_renewal_date_returns_pending(self):
        record = {"signed": False, "opted_out": False}
        assert status_for_record(record, date.today().isoformat()) == STATUS_PENDING

    @pytest.mark.parametrize("days_offset,expected_status", [
        (-7,  STATUS_OVERDUE),   # one week past deadline
        (-1,  STATUS_OVERDUE),   # yesterday
        (0,   STATUS_PENDING),   # today is NOT yet overdue (strict < comparison)
        (1,   STATUS_PENDING),   # tomorrow
        (14,  STATUS_PENDING),   # two weeks out
    ])
    def test_boundary_dates(self, days_offset, expected_status):
        """Verify the exact boundary between Overdue and Pending."""
        renewal = (date.today() + timedelta(days=days_offset)).isoformat()
        record = {"signed": False, "opted_out": False, "renewal_date": renewal}
        assert status_for_record(record, date.today().isoformat()) == expected_status


# ---------------------------------------------------------------------------
# Column schema integrity
# ---------------------------------------------------------------------------

class TestColumnSchema:
    def test_col_index_matches_columns_list(self):
        """COL_INDEX must mirror the position of every entry in COLUMNS."""
        for i, col in enumerate(COLUMNS):
            assert COL_INDEX[col] == i, f"{col} has wrong index in COL_INDEX"

    def test_required_fields_are_present(self):
        required = {
            "person_id", "name", "email", "phone",
            "group", "form_type", "renewal_date",
            "status", "opted_out", "notes",
        }
        assert required.issubset(set(COLUMNS))

    def test_exactly_12_columns(self):
        """Sheet has columns A–L (12 total). Changing this breaks the sync."""
        assert len(COLUMNS) == 12

    def test_no_duplicate_column_names(self):
        assert len(COLUMNS) == len(set(COLUMNS))
