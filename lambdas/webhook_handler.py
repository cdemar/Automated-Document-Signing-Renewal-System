"""BoldSign webhook handler.

API Gateway: POST /webhook. Fires on DocumentCompleted events.

Order of operations matters for idempotency: the conditional DynamoDB write
happens BEFORE the PDF upload, so a duplicate webhook delivery is rejected
by the ConditionExpression and never produces a second PDF in S3.

Hard constraints honored here:
- HMAC signature verified before ANY processing of the payload
- Signed PDFs uploaded with ObjectLockMode='COMPLIANCE'; retention period is
  per-form via form_config.json (background checks: 10 years)
- DynamoDB write uses ConditionExpression for idempotency
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from dateutil.relativedelta import relativedelta

import signing_platform

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
BUCKET = os.environ.get("BUCKET_NAME", "YOUR_BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME", "people_forms")
SYNC_FUNCTION = os.environ.get("SYNC_FUNCTION", "sync_to_sheets")
WEBHOOK_SECRET_NAME = "boldsign-webhook-secret"
CONFIG_KEY = "config/form_config.json"
DEFAULT_RETENTION_YEARS = 7
# BoldSign signs payloads Stripe-style: reject events older than this to
# prevent replay of captured requests.
SIGNATURE_TOLERANCE_SECONDS = 300

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
ses = boto3.client("ses", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
secrets_client = boto3.client("secretsmanager", region_name=REGION)

_webhook_secret = None


def _get_webhook_secret():
    global _webhook_secret
    if _webhook_secret is None:
        secret = secrets_client.get_secret_value(SecretId=WEBHOOK_SECRET_NAME)
        _webhook_secret = json.loads(secret["SecretString"])["webhook_secret"]
    return _webhook_secret


def _verify_signature(headers, raw_body):
    """Verify the X-BoldSign-Signature header (t=<unix ts>,s0=<hmac sha256>)."""
    header = headers.get("x-boldsign-signature", "")
    parts = dict(
        item.strip().split("=", 1) for item in header.split(",") if "=" in item
    )
    timestamp, signature = parts.get("t"), parts.get("s0")
    if not timestamp or not signature:
        return False
    try:
        age = datetime.now(timezone.utc).timestamp() - int(timestamp)
    except ValueError:
        return False
    if abs(age) > SIGNATURE_TOLERANCE_SECONDS:
        logger.warning("Rejected: timestamp age %ss exceeds tolerance", round(age))
        return False
    signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
    expected = hmac.new(
        _get_webhook_secret().encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    match = hmac.compare_digest(expected, signature)
    if not match:
        logger.warning("Rejected: HMAC mismatch (secret wrong or payload tampered)")
    return match


def _safe_name(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "unknown"


def _load_config():
    obj = s3.get_object(Bucket=BUCKET, Key=CONFIG_KEY)
    return json.loads(obj["Body"].read())


def _retention_years(config, record):
    """Object Lock duration for this record's form.

    Per-form `retention_years` in form_config.json wins, then the config's
    global `retention_years`, then the hardcoded default. Object Lock
    retention is per-object, so forms with different legal retention periods
    coexist in the one bucket.
    """
    form_cfg = (
        config.get("groups", {})
        .get(record.get("group"), {})
        .get("forms", {})
        .get(record.get("form_type"), {})
    )
    return int(
        form_cfg.get("retention_years")
        or config.get("retention_years")
        or DEFAULT_RETENTION_YEARS
    )


def _send_countersign_email(config, record, document_id):
    """Email the group owner their sign link after the primary signer completes."""
    group_cfg = config.get("groups", {}).get(record.get("group"), {})
    owner_email = group_cfg.get("owner_email")
    if not owner_email:
        return
    owner_name = group_cfg.get("owner_name", "Church Representative")
    form_cfg = group_cfg.get("forms", {}).get(record.get("form_type"), {})
    form_name = form_cfg.get("display_name", record.get("form_type", ""))
    sign_link = signing_platform.get_sign_link(document_id, owner_email)
    ses.send_email(
        Source=config["ses_from"],
        Destination={"ToAddresses": [owner_email]},
        Message={
            "Subject": {
                "Data": f"Countersignature needed: {form_name} — {record.get('name', '')}",
                "Charset": "UTF-8",
            },
            "Body": {"Text": {"Data": (
                f"Hi {owner_name},\n\n"
                f"{record.get('name', 'A participant')} has completed their {form_name}. "
                f"Your countersignature is now required to finalize it.\n\n"
                f"Sign here:\n{sign_link}\n\n"
                f"Thank you,\n{config['ses_from']}"
            ), "Charset": "UTF-8"}},
        },
    )
    logger.info("Sent countersign email for document %s to %s", document_id, owner_email)


def _find_record(table, document_id, metadata):
    """Locate the DynamoDB record for this document.

    Primary path: person_id/form_id stored as BoldSign metaData at send time.
    Fallback: scan for the stored document_id (fine at this table size).
    """
    person_id = (metadata or {}).get("person_id")
    form_id = (metadata or {}).get("form_id")
    if person_id and form_id:
        item = table.get_item(Key={"person_id": person_id, "form_id": form_id}).get("Item")
        if item:
            return item
    scan = table.scan(
        FilterExpression="document_id = :d",
        ExpressionAttributeValues={":d": document_id},
    )
    items = scan.get("Items", [])
    return items[0] if items else None


def lambda_handler(event, context):
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # BoldSign sends a verification POST with an empty or minimal body to confirm
    # the endpoint is reachable. Respond 200 before attempting HMAC verification.
    if not raw_body or raw_body.strip() in ("{}", ""):
        return {"statusCode": 200, "body": "ok"}

    # Verification pings arrive without a signature header (webhook secret not yet
    # established). Accept them so BoldSign can save the webhook configuration.
    if not headers.get("x-boldsign-signature"):
        return {"statusCode": 200, "body": "ok"}

    if not _verify_signature(headers, raw_body):
        logger.warning("Rejected webhook: invalid signature")
        return {"statusCode": 401, "body": "invalid signature"}

    payload = json.loads(raw_body)
    event_type = (payload.get("event") or {}).get("eventType", "")
    if event_type not in ("Completed", "DocumentCompleted", "Signed", "DocumentSigned"):
        # BoldSign verification pings and unrelated events: acknowledge and skip.
        return {"statusCode": 200, "body": f"ignored event {event_type}"}

    document = payload.get("document") or payload.get("data") or {}
    document_id = document.get("documentId")
    if not document_id:
        logger.error("Event %s missing documentId: %s", event_type, json.dumps(payload)[:1000])
        return {"statusCode": 400, "body": "missing documentId"}

    config = _load_config()
    table = dynamodb.Table(TABLE_NAME)
    record = _find_record(table, document_id, document.get("metaData"))
    if not record:
        logger.error("No DynamoDB record found for document %s", document_id)
        # 200 so BoldSign doesn't retry forever; the error is in CloudWatch.
        return {"statusCode": 200, "body": "no matching record"}

    if event_type in ("Signed", "DocumentSigned"):
        form_cfg = (
            config.get("groups", {})
            .get(record.get("group"), {})
            .get("forms", {})
            .get(record.get("form_type"), {})
        )
        if form_cfg.get("countersign", False):
            _send_countersign_email(config, record, document_id)
        return {"statusCode": 200, "body": "signed event acknowledged"}

    signed_at = datetime.now(timezone.utc)
    retain_until = signed_at + relativedelta(years=_retention_years(config, record))
    signer_name = record.get("name", "unknown")
    s3_key = (
        f"{record['group']}/{record['form_type']}/"
        f"{signed_at.strftime('%Y-%m')}/{document_id}_{_safe_name(signer_name)}.pdf"
    )

    # Claim the record before touching S3 — duplicates stop here.
    try:
        table.update_item(
            Key={"person_id": record["person_id"], "form_id": record["form_id"]},
            UpdateExpression=(
                "SET signed = :true, signed_at = :ts, document_id = :doc, "
                "s3_key = :key, retain_until = :ret"
            ),
            ConditionExpression="attribute_not_exists(signed_at)",
            ExpressionAttributeValues={
                ":true": True,
                ":ts": signed_at.isoformat(),
                ":doc": document_id,
                ":key": s3_key,
                ":ret": retain_until.isoformat(),
            },
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("Duplicate webhook for document %s — already processed", document_id)
        return {"statusCode": 200, "body": "duplicate ignored"}

    pdf_bytes = signing_platform.download_pdf(document_id)
    s3.put_object(
        Bucket=BUCKET,
        Key=s3_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=retain_until,
    )

    # Audit metadata sits next to the PDF, intentionally without Object Lock.
    metadata_key = s3_key.rsplit("/", 1)[0] + f"/{document_id}_metadata.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=metadata_key,
        Body=json.dumps(
            {
                "document_id": document_id,
                "person_id": record["person_id"],
                "form_id": record["form_id"],
                "signer_name": signer_name,
                "signer_email": record.get("email"),
                "group": record["group"],
                "form_type": record["form_type"],
                "signed_at": signed_at.isoformat(),
                "retain_until": retain_until.isoformat(),
                "s3_key": s3_key,
            },
            indent=2,
        ),
        ContentType="application/json",
    )

    lambda_client.invoke(FunctionName=SYNC_FUNCTION, InvocationType="Event", Payload=b"{}")
    logger.info("Stored signed PDF %s for %s", s3_key, record["person_id"])
    return {"statusCode": 200, "body": "ok"}
