"""One-time CSV import for seeding the initial 250 people into DynamoDB.

Run locally (not a Lambda):

    python bulk_import.py people.csv [--dry-run]

Expected CSV columns (header row required):
    name, email, phone, group, form_id, form_type, renewal_date, dob

Duplicate protection: a row whose lowercased email + form_id already exists
in the table is skipped, so the script is safe to re-run after a partial
failure. After importing, run the sync_to_sheets Lambda to populate the
dashboard.
"""

import argparse
import csv
import sys
import uuid
from datetime import date, datetime, timezone

import boto3

REGION = "us-east-2"
TABLE_NAME = "people_forms"
REQUIRED_COLUMNS = {"name", "email", "phone", "group", "form_id", "form_type", "renewal_date"}


def existing_email_form_pairs(table):
    pairs, kwargs = set(), {"ProjectionExpression": "email, form_id"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            pairs.add((item.get("email", ""), item.get("form_id", "")))
        if "LastEvaluatedKey" not in page:
            return pairs
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    parser = argparse.ArgumentParser(description="Seed people_forms from a CSV file")
    parser.add_argument("csv_file")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and report without writing to DynamoDB")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    seen = existing_email_form_pairs(table) if not args.dry_run else set()

    imported = skipped = errors = 0
    with open(args.csv_file, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for line_no, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            form_id = (row.get("form_id") or "").strip()
            renewal = (row.get("renewal_date") or "").strip()

            if not name or not email or not form_id:
                print(f"  line {line_no}: missing name/email/form_id — SKIPPED")
                errors += 1
                continue
            try:
                date.fromisoformat(renewal)
            except ValueError:
                print(f"  line {line_no}: renewal_date {renewal!r} is not YYYY-MM-DD — SKIPPED")
                errors += 1
                continue
            if (email, form_id) in seen:
                skipped += 1
                continue

            item = {
                "person_id": str(uuid.uuid4()),
                "form_id": form_id,
                "name": name,
                "email": email,
                "phone": (row.get("phone") or "").strip(),
                "group": (row.get("group") or "").strip(),
                "form_type": (row.get("form_type") or "").strip(),
                "renewal_date": renewal,
                "signed": False,
                "opted_out": False,
                "initial_email_sent": False,
                "reminder_email_sent": False,
                "owner_notified": False,
                "notes": (row.get("notes") or "").strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": "bulk_import",
            }
            if not args.dry_run:
                table.put_item(Item=item)
            seen.add((email, form_id))
            imported += 1

    label = "validated" if args.dry_run else "imported"
    print(f"\n{imported} {label}, {skipped} already existed, {errors} rows had errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
