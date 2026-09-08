"""Google Sheets → DynamoDB import. Runs every 15 minutes via EventBridge.

Two jobs:
1. New people: a row the owner typed in with column A (person_id) left blank
   becomes a new DynamoDB record. The generated person_id is written back to
   the sheet so the row is never imported twice.
2. Owner edits: for existing rows, the owner-editable fields (name, email,
   phone, renewal_date, notes) are copied into DynamoDB when changed.

Status fields (H–K) are never read from the sheet — DynamoDB owns those and
sync_to_sheets overwrites them. Every change is appended to the Audit Log tab.
"""

import json
import logging
import os
import uuid
from datetime import date, datetime, timezone

import boto3

import sheets_helper

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
BUCKET = os.environ.get("BUCKET_NAME", "YOUR_BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME", "people_forms")
CONFIG_KEY = "config/form_config.json"

EDITABLE_FIELDS = ["name", "email", "phone", "renewal_date", "notes"]

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def _load_config():
    obj = s3.get_object(Bucket=BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read())


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _valid_date(value):
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _new_person(table, sheet_id, group, group_cfg, row):
    name = row["name"].strip()
    email = row["email"].strip().lower()
    form_type = (row["form_type"].strip() or next(iter(group_cfg["forms"])))
    renewal_date = row["renewal_date"].strip()

    if not name or not email:
        logger.warning("Sheet row %s missing name/email — skipping", row["_row"])
        return None
    if form_type not in group_cfg["forms"]:
        logger.warning("Sheet row %s has unknown form_type %r — skipping", row["_row"], form_type)
        return None
    if renewal_date and not _valid_date(renewal_date):
        logger.warning("Sheet row %s has bad renewal_date %r — skipping", row["_row"], renewal_date)
        return None

    person_id = str(uuid.uuid4())
    form_id = group_cfg["forms"][form_type]["boldsign_template_id"]
    table.put_item(
        Item={
            "person_id": person_id,
            "form_id": form_id,
            "name": name,
            "email": email,
            "phone": row["phone"].strip(),
            "group": group,
            "form_type": form_type,
            "renewal_date": renewal_date,
            "signed": False,
            "opted_out": False,
            "initial_email_sent": False,
            "reminder_email_sent": False,
            "owner_notified": False,
            "notes": row["notes"],
            "created_at": _now_iso(),
            "created_by": group_cfg.get("owner_email", "sheet"),
        },
        ConditionExpression="attribute_not_exists(person_id)",
    )
    sheets_helper.update_cell(sheet_id, row["_row"], "person_id", person_id)
    sheets_helper.append_audit(
        sheet_id, _now_iso(), f"{name} <{email}>", "Added via sheet",
        group_cfg.get("owner_email", "sheet"),
    )
    return person_id


def _apply_edits(table, sheet_id, group_cfg, row, item):
    changes = {}
    for field in EDITABLE_FIELDS:
        sheet_value = row[field].strip() if field != "notes" else row[field]
        if field == "email":
            sheet_value = sheet_value.lower()
        if field == "renewal_date" and sheet_value and not _valid_date(sheet_value):
            logger.warning("Row %s: bad renewal_date %r — ignoring edit", row["_row"], sheet_value)
            continue
        if sheet_value != (item.get(field) or ""):
            changes[field] = sheet_value
    if not changes:
        return False

    sets, values, names = [], {}, {}
    for i, (field, value) in enumerate(changes.items()):
        # 'name' and 'notes' are DynamoDB reserved-ish; alias everything.
        sets.append(f"#a{i} = :v{i}")
        names[f"#a{i}"] = field
        values[f":v{i}"] = value
    table.update_item(
        Key={"person_id": item["person_id"], "form_id": item["form_id"]},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    sheets_helper.append_audit(
        sheet_id, _now_iso(), f"{item.get('name')} <{item.get('email')}>",
        "Edited: " + ", ".join(changes), "sheets-sync",
    )
    return True


def lambda_handler(event, context):
    config = _load_config()
    table = dynamodb.Table(TABLE_NAME)
    added = updated = 0

    for group, group_cfg in config["groups"].items():
        sheet_id = group_cfg.get("sheet_id")
        if not sheet_id or sheet_id.startswith("REPLACE"):
            continue
        for row in sheets_helper.read_people_rows(sheet_id):
            person_id = row["person_id"].strip()
            if not person_id:
                if any(row[f].strip() for f in ("name", "email")):
                    if _new_person(table, sheet_id, group, group_cfg, row):
                        added += 1
                continue
            form_id = group_cfg["forms"].get(
                row["form_type"].strip(), {}
            ).get("boldsign_template_id")
            if not form_id:
                continue
            item = table.get_item(
                Key={"person_id": person_id, "form_id": form_id}
            ).get("Item")
            if not item:
                logger.warning("Sheet row %s references unknown person_id %s",
                               row["_row"], person_id)
                continue
            if _apply_edits(table, sheet_id, group_cfg, row, item):
                updated += 1

    result = {"added": added, "updated": updated}
    logger.info("Sheet import complete: %s", json.dumps(result))
    return result
