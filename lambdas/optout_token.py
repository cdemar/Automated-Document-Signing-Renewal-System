"""HMAC opt-out token signing — prevents opt-out by ID guessing.

Goes in the Lambda Layer with sheets_helper.py and signing_platform.py:
reminder_checker and bulk_send mint tokens for email links, optout_handler
verifies them.

Tokens are HMAC-SHA256 over "optout:{person_id}|{form_id}" keyed with the
boldsign-webhook-secret. The "optout:" context prefix keeps these tokens
domain-separated from BoldSign webhook signatures even though they share a
secret (the roadmap's infrastructure defines exactly three secrets).
"""

import hashlib
import hmac
import json

import boto3

REGION = "us-east-2"
SECRET_NAME = "boldsign-webhook-secret"

_secret = None


def _get_secret():
    global _secret
    if _secret is None:
        client = boto3.client("secretsmanager", region_name=REGION)
        value = client.get_secret_value(SecretId=SECRET_NAME)
        _secret = json.loads(value["SecretString"])["webhook_secret"]
    return _secret


def make_token(person_id, form_id):
    message = f"optout:{person_id}|{form_id}".encode("utf-8")
    return hmac.new(_get_secret().encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_token(person_id, form_id, token):
    return hmac.compare_digest(make_token(person_id, form_id), token or "")


def optout_url(api_base_url, person_id, form_id):
    token = make_token(person_id, form_id)
    return f"{api_base_url}/optout?pid={person_id}&fid={form_id}&token={token}"
