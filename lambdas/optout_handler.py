"""Opt-out link handler.

API Gateway: GET /optout?pid=<person_id>&fid=<form_id>&token=<hmac>

The HMAC token is verified before any database access, so person IDs cannot
be guessed or enumerated. On success: marks the record opted out, cancels the
in-flight BoldSign document if there is one, and triggers a sheet sync so the
dashboard flips to 🔴 Declined. Opting out immediately stops all further
signer emails (reminder_checker skips opted-out records).
"""

import logging
import os
from datetime import datetime, timezone

import boto3

import optout_token
import signing_platform

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
TABLE_NAME = os.environ.get("TABLE_NAME", "people_forms")
SYNC_FUNCTION = os.environ.get("SYNC_FUNCTION", "sync_to_sheets")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:Georgia,serif;max-width:34rem;margin:4rem auto;padding:0 1rem;color:#333}}</style>
</head><body><h2>{title}</h2><p>{message}</p></body></html>"""


def _html(status, title, message):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": PAGE.format(title=title, message=message),
    }


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    person_id = params.get("pid", "")
    form_id = params.get("fid", "")
    token = params.get("token", "")

    if not optout_token.verify_token(person_id, form_id, token):
        logger.warning("Rejected opt-out: invalid token for pid=%s", person_id)
        return _html(403, "Link not valid", "This opt-out link is not valid or has been altered. "
                     "Please use the link from your email, or contact the church office.")

    table = dynamodb.Table(TABLE_NAME)
    record = table.get_item(Key={"person_id": person_id, "form_id": form_id}).get("Item")
    if not record:
        return _html(404, "Record not found", "We couldn't find your record. "
                     "Please contact the church office.")

    if record.get("opted_out"):
        return _html(200, "Already opted out", "You had already opted out — no further "
                     "reminders will be sent. Nothing more to do.")

    table.update_item(
        Key={"person_id": person_id, "form_id": form_id},
        UpdateExpression="SET opted_out = :true, opted_out_at = :ts",
        ExpressionAttributeValues={
            ":true": True,
            ":ts": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Cancel the pending BoldSign envelope so it can't be signed later.
    document_id = record.get("document_id")
    if document_id and not record.get("signed"):
        try:
            signing_platform.cancel(document_id, reason="Signer opted out")
        except signing_platform.SigningPlatformError as err:
            # Opt-out itself succeeded; a stale envelope is harmless. Log and move on.
            logger.warning("Could not cancel document %s: %s", document_id, err)

    lambda_client.invoke(FunctionName=SYNC_FUNCTION, InvocationType="Event", Payload=b"{}")
    logger.info("Opt-out recorded for person %s form %s", person_id, form_id)
    return _html(200, "You're opted out", "You will receive no further reminder emails about "
                 "this form. If this was a mistake, contact the church office and they can "
                 "re-enroll you.")
