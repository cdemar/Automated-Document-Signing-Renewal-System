"""Shared Google Sheets utilities for the church forms system.

Deployed inside the Lambda Layer (church-forms-dependencies) alongside
google-api-python-client, google-auth, and python-dateutil.

The People tab column order is load-bearing: every function here addresses
columns by index position. Changing the sheet layout requires changing
COLUMNS below and nothing else.
"""

import json

import boto3
from google.oauth2 import service_account
from googleapiclient.discovery import build

REGION = "us-east-2"
SHEETS_SECRET_NAME = "google-sheets-credentials"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PEOPLE_TAB = "People"
AUDIT_TAB = "Audit Log"

# Column order A–L. sheets are read/written as positional lists in this order.
COLUMNS = [
    "person_id",       # A
    "name",            # B
    "email",           # C
    "phone",           # D
    "group",           # E
    "form_type",       # F
    "renewal_date",    # G
    "status",          # H
    "last_reminder",   # I
    "reminder_count",  # J
    "opted_out",       # K
    "notes",           # L
]
COL_INDEX = {name: i for i, name in enumerate(COLUMNS)}
LAST_COL_LETTER = "L"

STATUS_RENEWED = "🟢 Renewed"
STATUS_PENDING = "🟡 Pending"
STATUS_DECLINED = "🔴 Declined"
STATUS_OVERDUE = "⚠️ Overdue"

STATUS_COLORS = {
    STATUS_RENEWED:  "#d9ead3",
    STATUS_PENDING:  "#fff9c4",
    STATUS_DECLINED: "#fce8e6",
    STATUS_OVERDUE:  "#fde8c8",
}


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    return {
        "red":   int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue":  int(h[4:6], 16) / 255,
    }


def _get_tab_id(service, sheet_id, tab_name):
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Tab '{tab_name}' not found")

_service = None


def get_sheets_service():
    """Build (and cache for the Lambda container) the Sheets API client."""
    global _service
    if _service is None:
        secrets = boto3.client("secretsmanager", region_name=REGION)
        secret = secrets.get_secret_value(SecretId=SHEETS_SECRET_NAME)
        info = json.loads(secret["SecretString"])
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def read_people_rows(sheet_id):
    """Return all data rows from the People tab as dicts keyed by COLUMNS.

    Each dict also carries '_row' — the 1-based sheet row number — so callers
    can write back to the exact row they read.
    """
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{PEOPLE_TAB}!A4:{LAST_COL_LETTER}",
    ).execute()
    rows = []
    for offset, raw in enumerate(result.get("values", [])):
        padded = raw + [""] * (len(COLUMNS) - len(raw))
        row = dict(zip(COLUMNS, padded))
        row["_row"] = offset + 4  # rows 1-3 are header, example, and spacer
        rows.append(row)
    return rows


def overwrite_people_rows(sheet_id, records):
    """Replace all data rows in the People tab with `records` (list of dicts).

    Clears the old data range first so deleted records don't leave stale rows.
    Row background colors are updated to match each record's status.
    """
    service = get_sheets_service()
    values = [[str(rec.get(col, "")) for col in COLUMNS] for rec in records]
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"{PEOPLE_TAB}!A4:{LAST_COL_LETTER}",
    ).execute()
    if values:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{PEOPLE_TAB}!A4",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    tab_id = _get_tab_id(service, sheet_id, PEOPLE_TAB)
    num_cols = len(COLUMNS)
    requests = []

    for i, rec in enumerate(records):
        color = STATUS_COLORS.get(rec.get("status", ""), "#ffffff")
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": tab_id,
                    "startRowIndex": i + 3,
                    "endRowIndex": i + 4,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": _hex_to_rgb(color)}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    # Clear colors for any rows beyond the current data (handles shrinking list)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": tab_id,
                "startRowIndex": len(records) + 3,
                "endRowIndex": 1000,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": _hex_to_rgb("#ffffff")}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": requests},
    ).execute()


def update_cell(sheet_id, row_number, column_name, value):
    """Write a single cell in the People tab (used to backfill person_id)."""
    service = get_sheets_service()
    col_letter = chr(ord("A") + COL_INDEX[column_name])
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{PEOPLE_TAB}!{col_letter}{row_number}",
        valueInputOption="RAW",
        body={"values": [[str(value)]]},
    ).execute()


def append_audit(sheet_id, timestamp, person, action, changed_by):
    """Append one row to the Audit Log tab: Timestamp | Person | Action | Changed By."""
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{AUDIT_TAB}!A:D",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[timestamp, person, action, changed_by]]},
    ).execute()


def status_for_record(record, today_iso):
    """Map a DynamoDB record to the dashboard status value."""
    if record.get("signed"):
        return STATUS_RENEWED
    if record.get("opted_out"):
        return STATUS_DECLINED
    renewal = record.get("renewal_date") or ""
    if renewal and renewal < today_iso:
        return STATUS_OVERDUE
    return STATUS_PENDING
