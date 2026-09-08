#!/usr/bin/env bash
#
# Creates the S3 bucket for signed church forms with Object Lock enabled.
#
# MUST be run before any other infrastructure step: Object Lock can only be
# enabled at bucket creation time and can never be added to an existing bucket.
#
# Usage: ./create_bucket.sh [bucket-name]

set -euo pipefail

BUCKET="${1:?Usage: ./create_bucket.sh YOUR_BUCKET_NAME}"
REGION="${AWS_REGION:-us-east-2}"

echo "Creating bucket '${BUCKET}' in ${REGION} with Object Lock enabled..."
aws s3api create-bucket \
  --bucket "${BUCKET}" \
  --region "${REGION}" \
  --create-bucket-configuration LocationConstraint="${REGION}" \
  --object-lock-enabled-for-bucket

# Object Lock requires versioning; AWS enables it automatically, but verify.
STATUS=$(aws s3api get-bucket-versioning --bucket "${BUCKET}" --query Status --output text)
if [[ "${STATUS}" != "Enabled" ]]; then
  echo "ERROR: versioning is '${STATUS}', expected 'Enabled'. Object Lock is broken — investigate before continuing." >&2
  exit 1
fi

echo "Blocking all public access..."
aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "Enforcing encryption at rest (SSE-S3)..."
aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
    '{"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}'

# No default Object Lock retention is set here on purpose: signed PDFs get a
# per-form COMPLIANCE lock (background checks: 10 years), applied per-object
# by webhook_handler.py. Metadata JSON and config files must remain unlocked.

OBJECT_LOCK=$(aws s3api get-object-lock-configuration --bucket "${BUCKET}" \
  --query 'ObjectLockConfiguration.ObjectLockEnabled' --output text)
echo ""
echo "Bucket created. Object Lock: ${OBJECT_LOCK}, Versioning: ${STATUS}, Region: ${REGION}"
echo "Next step: upload form_config.json to s3://${BUCKET}/config/form_config.json"
