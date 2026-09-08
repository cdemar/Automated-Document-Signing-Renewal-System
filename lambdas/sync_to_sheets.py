"""DynamoDB → Google Sheets sync.

Runs daily at 7am PST via EventBridge, and is also invoked asynchronously by
webhook_handler and optout_handler so the dashboard updates within minutes of
a signature or opt-out.

DynamoDB is the source of truth for status fields (H–K); the sheet's People
tab is rewritten from it on every run. Owner-editable fields (name, email,
phone, renewal_date, notes) flow the OTHER way via sheets_to_dynamo — this
function writes back whatever DynamoDB currently holds, so owner edits made
between syncs are picked up by the 15-minute import before they could be
overwritten here.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3

import sheets_helper

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
BUCKET = os.environ.get("BUCKET_NAME", "YOUR_BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME", "people_forms")
CONFIG_KEY = "config/form_config.json"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def _load_config():
    obj = s3.get_object(Bucket=BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read())


def _scan_all(table):
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def lambda_handler(event, context):
    config = _load_config()
    table = dynamodb.Table(TABLE_NAME)
    today_iso = datetime.now(LOCAL_TZ).date().isoformat()

    by_group = {}
    for record in _scan_all(table):
        by_group.setdefault(record.get("group", "unknown"), []).append(record)

    synced = {}
    for group, group_cfg in config["groups"].items():
        sheet_id = group_cfg.get("sheet_id")
        if not sheet_id or sheet_id.startswith("REPLACE"):
            logger.warning("Group %s has no sheet_id configured — skipping", group)
            continue
        records = sorted(by_group.get(group, []), key=lambda r: r.get("name", ""))
        rows = [
            {
                "person_id": r["person_id"],
                "name": r.get("name", ""),
                "email": r.get("email", ""),
                "phone": r.get("phone", ""),
                "group": r.get("group", ""),
                "form_type": r.get("form_type", ""),
                "renewal_date": r.get("renewal_date", ""),
                "status": sheets_helper.status_for_record(r, today_iso),
                "last_reminder": r.get("last_reminder", ""),
                "reminder_count": int(r.get("reminder_count") or 0),
                "opted_out": "Yes" if r.get("opted_out") else "",
                "notes": r.get("notes", ""),
            }
            for r in records
        ]
        sheets_helper.overwrite_people_rows(sheet_id, rows)
        synced[group] = len(rows)

    logger.info("Sheet sync complete: %s", json.dumps(synced))
    return synced
