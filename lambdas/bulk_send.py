"""Initial bulk send — creates BoldSign envelopes and emails sign links.

Run locally (not a Lambda) after bulk_import.py has seeded DynamoDB:

    python bulk_send.py --limit 5                  # Phase 7: 5-person test
    python bulk_send.py --emails a@x.org b@y.org   # specific people only
    python bulk_send.py --all                      # Phase 8: full launch
    python bulk_send.py --all --dry-run            # show who would be sent

All BoldSign calls go through signing_platform.py. BoldSign's own emails are
disabled; this script sends the day -14-style sign-link email via SES (with
the HMAC opt-out link) and marks initial_email_sent so reminder_checker
doesn't double-send. Records already sent, signed, or opted out are skipped,
making the script safe to re-run.

Requires sheets-layer modules on the path; run from the lambdas/ folder.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import boto3

import optout_token
import signing_platform

REGION = "us-east-2"
BUCKET = os.environ.get("BUCKET_NAME", "YOUR_BUCKET_NAME")
TABLE_NAME = "people_forms"
CONFIG_KEY = "config/form_config.json"
# BoldSign rate limit headroom: pause between sends so 250 envelopes
# don't trip API throttling.
SECONDS_BETWEEN_SENDS = 1.0


def load_config():
    s3 = boto3.client("s3", region_name=REGION)
    obj = s3.get_object(Bucket=BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read())


def scan_all(table):
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def send_one(config, table, ses, record):
    group_cfg = config["groups"][record["group"]]
    form_cfg = group_cfg["forms"][record["form_type"]]
    form_name = form_cfg.get("display_name", record["form_type"])

    document_id = signing_platform.send_from_template(
        template_id=form_cfg["boldsign_template_id"],
        signer_name=record["name"],
        signer_email=record["email"],
        title=form_name,
        message="Please sign at the link emailed to you.",
        representative_name=group_cfg.get("owner_name"),
        representative_email=group_cfg.get("owner_email"),
        metadata={"person_id": record["person_id"], "form_id": record["form_id"]},
    )
    sign_link = signing_platform.get_sign_link(document_id, record["email"])
    opt_url = optout_token.optout_url(
        config["api_base_url"], record["person_id"], record["form_id"]
    )
    ses.send_email(
        Source=config["ses_from"],
        Destination={"ToAddresses": [record["email"]]},
        Message={
            "Subject": {
                "Data": f"Please sign: {form_name} due {record['renewal_date']}",
                "Charset": "UTF-8",
            },
            "Body": {"Text": {"Data": (
                f"Hi {record['name']},\n\n"
                f"It's time to complete your {form_name} form. "
                f"Please sign by {record['renewal_date']}.\n\n"
                f"Sign here (takes about a minute):\n{sign_link}\n\n"
                f"If you no longer serve in this role and this doesn't apply to you, "
                f"you can opt out here:\n{opt_url}\n\n"
                f"Thank you,\n{config['ses_from']}"
            ), "Charset": "UTF-8"}},
        },
    )
    table.update_item(
        Key={"person_id": record["person_id"], "form_id": record["form_id"]},
        UpdateExpression=(
            "SET document_id = :d, initial_email_sent = :t, "
            "last_reminder = :lr, reminder_count = :rc"
        ),
        ExpressionAttributeValues={
            ":d": document_id,
            ":t": True,
            ":lr": datetime.now(timezone.utc).date().isoformat(),
            ":rc": int(record.get("reminder_count") or 0) + 1,
        },
    )
    return document_id


def main():
    parser = argparse.ArgumentParser(description="Send forms via BoldSign + SES")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="send to every eligible person")
    scope.add_argument("--limit", type=int, help="send to at most N eligible people")
    scope.add_argument("--emails", nargs="+", help="send only to these email addresses")
    parser.add_argument("--dry-run", action="store_true", help="list recipients, send nothing")
    args = parser.parse_args()

    config = load_config()
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    ses = boto3.client("ses", region_name=REGION)

    wanted = {e.lower() for e in args.emails} if args.emails else None
    eligible = [
        r for r in scan_all(table)
        if not r.get("signed")
        and not r.get("opted_out")
        and not r.get("initial_email_sent")
        and not r.get("document_id")
        and r.get("group") in config["groups"]
        and r.get("form_type") in config["groups"][r["group"]]["forms"]
        and (wanted is None or r.get("email") in wanted)
    ]
    eligible.sort(key=lambda r: r.get("name", ""))
    if args.limit:
        eligible = eligible[: args.limit]

    if not eligible:
        print("No eligible records to send (already sent/signed/opted out, or no match).")
        return
    print(f"{len(eligible)} people to send:")
    for r in eligible:
        print(f"  {r['name']} <{r['email']}> — {r['group']}/{r['form_type']} "
              f"due {r.get('renewal_date', '?')}")
    if args.dry_run:
        print("\nDry run — nothing sent.")
        return

    sent = failed = 0
    for r in eligible:
        try:
            document_id = send_one(config, table, ses, r)
            print(f"  sent {r['email']} → document {document_id}")
            sent += 1
        except Exception as err:  # keep going; report failures at the end
            print(f"  FAILED {r['email']}: {err}", file=sys.stderr)
            failed += 1
        time.sleep(SECONDS_BETWEEN_SENDS)

    print(f"\n{sent} sent, {failed} failed.")
    if failed:
        print("Re-running this script will retry only the failed people.")
        sys.exit(1)


if __name__ == "__main__":
    main()
