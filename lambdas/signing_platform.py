"""BoldSign API abstraction layer.

Every BoldSign call in the system goes through this module so that swapping
signing platforms later means rewriting one file. Deployed inside the Lambda
Layer (church-forms-dependencies) alongside sheets_helper.py.

Uses urllib from the standard library — no extra dependencies required.
The API key is always fetched from Secrets Manager, never hardcoded.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import boto3

REGION = "us-east-2"
API_KEY_SECRET_NAME = "boldsign-api-key"
BASE_URL = "https://api.boldsign.com"

_api_key = None


class SigningPlatformError(Exception):
    """Raised when the signing platform returns an unexpected response."""


def _get_api_key():
    global _api_key
    if _api_key is None:
        secrets = boto3.client("secretsmanager", region_name=REGION)
        secret = secrets.get_secret_value(SecretId=API_KEY_SECRET_NAME)
        _api_key = json.loads(secret["SecretString"])["api_key"]
    return _api_key


def _request(method, path, query=None, body=None, raw_response=False):
    url = BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-KEY", _get_api_key())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise SigningPlatformError(
            f"BoldSign {method} {path} failed with HTTP {err.code}: {detail}"
        ) from err
    if raw_response:
        return payload
    return json.loads(payload) if payload else {}


def send_from_template(
    template_id, signer_name, signer_email, title, message,
    representative_name=None, representative_email=None, metadata=None,
):
    """Create and send a document from a template. Returns the document ID.

    BoldSign's own signer emails are disabled — reminder_checker sends all
    emails through SES so the 3-email flow and opt-out links stay under our
    control. `metadata` (e.g. person_id/form_id) is echoed back in webhook
    payloads so webhook_handler can match the document to a DynamoDB record.

    If `representative_email` is provided, a second signer role (Representative,
    roleIndex 2) is added after the primary signer. Signing order is preserved:
    the representative signs only after the primary signer completes.
    """
    roles = [
        {
            "roleIndex": 1,
            "roleType": "Signer",
            "signerName": signer_name,
            "signerEmail": signer_email,
        }
    ]
    if representative_email:
        roles.append(
            {
                "roleIndex": 2,
                "roleType": "Signer",
                "signerName": representative_name or "Church Representative",
                "signerEmail": representative_email,
            }
        )
    body = {
        "title": title,
        "message": message,
        "roles": roles,
        "disableEmails": True,
        "disableExpiryAlert": True,
        "metaData": {k: str(v) for k, v in (metadata or {}).items()},
    }
    result = _request("POST", "/v1/template/send", query={"templateId": template_id}, body=body)
    document_id = result.get("documentId")
    if not document_id:
        raise SigningPlatformError(f"BoldSign send returned no documentId: {result}")
    return document_id


def get_sign_link(document_id, signer_email, redirect_url=None):
    """Return the signing URL for a signer, for embedding in our SES emails."""
    query = {"documentId": document_id, "signerEmail": signer_email}
    if redirect_url:
        query["redirectUrl"] = redirect_url
    result = _request("GET", "/v1/document/getEmbeddedSignLink", query=query)
    sign_link = result.get("signLink")
    if not sign_link:
        raise SigningPlatformError(f"BoldSign returned no signLink for {document_id}: {result}")
    return sign_link


def get_status(document_id):
    """Return the document's status string (e.g. 'InProgress', 'Completed')."""
    result = _request("GET", "/v1/document/properties", query={"documentId": document_id})
    return result.get("status")


def cancel(document_id, reason="Cancelled by church forms automation"):
    """Revoke an in-progress document (e.g. when a signer opts out)."""
    _request(
        "POST",
        "/v1/document/revoke",
        query={"documentId": document_id},
        body={"message": reason},
    )


def download_pdf(document_id):
    """Download the completed, signed PDF as bytes."""
    return _request(
        "GET", "/v1/document/download", query={"documentId": document_id}, raw_response=True
    )
