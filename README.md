# Church Form Automation

An automated document signing and renewal system for nonprofits and churches. Handles sending, reminding, tracking, and storing legally required forms — built entirely on AWS serverless infrastructure using Python, BoldSign, and Google Sheets.

Designed for organizations that need to collect annual consent or compliance forms (such as background check waivers) from employees and volunteers, with a full audit trail and legally compliant document retention.

---

## What it does

- Sends signing requests via [BoldSign](https://boldsign.com) with a branded email and a one-click sign link
- Runs a 3-email reminder flow per person per annual cycle (initial → final reminder → weekly owner report)
- Tracks every person's status in a Google Sheet the form owner can view without AWS access
- Stores signed PDFs in S3 with **Object Lock (Compliance mode)** — documents cannot be deleted during the retention period, even by an AWS administrator
- Sends the form owner a **weekly HTML report every Friday** showing who signed, who is overdue (color-coded by first vs. repeat week), who opted out, and who is still pending
- Provides an opt-out link in every email for people who no longer serve in the relevant role

---

## Architecture

```
Google Sheets (form owner edits this)
      │
      ▼  every 15 min
sheets_to_dynamo ──► DynamoDB (people_forms)
                          │
                          ▼  daily 8am PST
                    reminder_checker ──► SES (emails to signers + owner)
                          │
                          ▼  on sign/complete
                    BoldSign webhook ──► webhook_handler
                                              │
                                              ├──► S3 (signed PDF, Object Lock)
                                              └──► sync_to_sheets ──► Google Sheets
```

**AWS services used:** Lambda (Python 3.13), DynamoDB, S3 (Object Lock), SES, API Gateway, EventBridge, Secrets Manager, IAM, CloudWatch

---

## Email flow

```
Day -14  Signer receives sign link email with "Sign Now →" button
Day -7   Signer receives final reminder
         Owner receives a list of everyone who hasn't signed yet
Every Friday
         Owner receives a full HTML weekly report:
           🟡 Yellow = new this week (first time overdue)
           🔴 Red    = still overdue from a prior week
           ✅ Green  = signed
           ⛔ Gray   = opted out
           🕐 Blue   = pending (not yet due)
```

Signing or opting out immediately stops all further signer emails.

---

## Repository structure

```
lambdas/
  webhook_handler.py     BoldSign webhook — saves signed PDF to S3, triggers sheet sync
  reminder_checker.py    Daily email runner — 3-email flow + Friday owner report
  sync_to_sheets.py      DynamoDB → Google Sheets sync
  sheets_to_dynamo.py    Google Sheets → DynamoDB import (runs every 15 min)
  optout_handler.py      Handles opt-out link clicks
  optout_token.py        HMAC opt-out token mint/verify (shared layer module)
  sheets_helper.py       Google Sheets utilities (shared layer module)
  signing_platform.py    BoldSign API abstraction layer (shared layer module)
  bulk_import.py         One-time bulk import of people from a CSV into DynamoDB
  bulk_send.py           One-time bulk send for initial launch

form_config.json         Configuration template — copy and fill in your values
create_bucket.sh         Creates the S3 bucket with Object Lock enabled
build_layer.sh           Builds the Lambda Layer zip
build_functions.sh       Zips Lambda functions for manual upload
```

---

## Prerequisites

- AWS account with permissions to create Lambda, DynamoDB, S3, SES, API Gateway, Secrets Manager, EventBridge, IAM, CloudWatch
- [BoldSign](https://boldsign.com) account (paid plan required for API access)
- Google Cloud project with Sheets API and Drive API enabled, and a service account JSON key
- A verified domain in AWS SES (production access required to send to non-verified addresses)
- Python 3.13

---

## Setup

### 1. Create the S3 bucket

Object Lock can only be enabled at creation time — run this first.

```bash
./create_bucket.sh YOUR_BUCKET_NAME
```

### 2. Store secrets in AWS Secrets Manager

Create three secrets (all in the same region you'll deploy to):

| Secret name | JSON shape |
|---|---|
| `boldsign-api-key` | `{"api_key": "YOUR_KEY"}` |
| `boldsign-webhook-secret` | `{"webhook_secret": "YOUR_SECRET"}` |
| `google-sheets-credentials` | paste the full Google service account JSON |

### 3. Configure `form_config.json`

Copy `form_config.json`, fill in every `YOUR_*` placeholder, then upload to S3:

```bash
aws s3 cp form_config.json s3://YOUR_BUCKET_NAME/config/form_config.json --region us-east-2
```

**Config fields:**

| Field | Description |
|---|---|
| `ses_from` | Verified SES sender address (e.g. `noreply@yourdomain.org`) |
| `admin_email` | Where CloudWatch alarm notifications go |
| `api_base_url` | Your API Gateway base URL (no trailing slash) |
| `s3_bucket` | Your S3 bucket name |
| `groups.<group>.owner_email` | Who receives the weekly Friday report |
| `groups.<group>.sheet_id` | Google Sheet ID (from the URL) |
| `forms.<form>.boldsign_template_id` | BoldSign template ID |
| `forms.<form>.retention_years` | How long signed PDFs are locked in S3 |
| `forms.<form>.countersign` | `true` if a second signer (representative) should countersign |

### 4. Build and deploy the Lambda Layer

The layer must be built on Amazon Linux — use AWS CloudShell, not a Mac.

In CloudShell:
```bash
rm -rf layer church-forms-dependencies.zip
mkdir -p layer/python
pip install google-api-python-client google-auth python-dateutil -t layer/python/ -q
```

Upload `sheets_helper.py`, `signing_platform.py`, and `optout_token.py` from `lambdas/` into CloudShell, then:

```bash
cp sheets_helper.py signing_platform.py optout_token.py layer/python/
cd layer
zip -r ../church-forms-dependencies.zip python/
cd ..
aws lambda publish-layer-version \
  --layer-name church-forms-dependencies \
  --zip-file fileb://church-forms-dependencies.zip \
  --compatible-runtimes python3.13 \
  --region us-east-2 \
  --query 'LayerVersionArn'
```

Attach the returned ARN to all 5 Lambda functions.

### 5. Create Lambda functions

Create 5 Lambda functions (Python 3.13, attach `church-forms-lambda-role`, attach the layer):

| Function name | File | Timeout | Memory | Trigger |
|---|---|---|---|---|
| `webhook_handler` | `lambdas/webhook_handler.py` | 60s | 256 MB | API Gateway POST /webhook |
| `reminder_checker` | `lambdas/reminder_checker.py` | 5 min | 128 MB | EventBridge daily 8am PST |
| `sync_to_sheets` | `lambdas/sync_to_sheets.py` | 2 min | 128 MB | EventBridge daily 7am PST |
| `sheets_to_dynamo` | `lambdas/sheets_to_dynamo.py` | 2 min | 128 MB | EventBridge every 15 min |
| `optout_handler` | `lambdas/optout_handler.py` | 10s | 128 MB | API Gateway GET /optout |

Set the `BUCKET_NAME` environment variable on each function to your S3 bucket name.

### 6. Create the DynamoDB table

Table name: `people_forms`
- Partition key: `person_id` (String)
- Sort key: `form_id` (String)

### 7. Create the Google Sheet

- Columns A–L in this exact order:
  `person_id | name | email | phone | group | form_type | renewal_date | status | last_reminder | reminder_count | opted_out | notes`
- Row 1: headers, Row 2: sub-labels, Row 3: example row (never touched by sync), Row 4+: live data
- Share with your Google service account (Editor)
- Share with the form owner (Editor)

### 8. Configure BoldSign

- Create a template for each form
- In webhook settings, enable `Signed` and `Completed` events, pointing to your API Gateway `/webhook` endpoint
- Copy the webhook signing secret into Secrets Manager

### 9. Set up SES

- Verify your sending domain in SES (us-east-2)
- Enable Easy DKIM (RSA 2048-bit) and add the DNS records to your domain registrar
- Request SES production access

---

## Adding a new form type

No code changes needed — the system is config-only for new forms.

1. Create a BoldSign template → copy the template ID
2. Add an entry under `forms` in `form_config.json` with the new template ID and retention period
3. Create a new Google Sheet (duplicate the existing one, clear data rows)
4. Add the sheet ID to `form_config.json` under the group
5. Upload the updated `form_config.json` to S3
6. Add people to the new sheet — the system picks them up automatically

---

## S3 storage and cost savings

Signed PDFs are compliance documents that are rarely accessed after signing. To reduce storage costs, add a lifecycle rule that automatically moves PDFs to a cheaper storage tier after 30 days (Object Lock is preserved in all tiers):

| Tier | Cost/GB/month | Retrieval time |
|---|---|---|
| S3 Standard | $0.023 | Instant |
| S3 Standard-IA | $0.0125 | Instant |
| S3 Glacier Instant Retrieval | $0.004 | Milliseconds |
| S3 Glacier Flexible Retrieval | $0.0036 | 3–5 hours (free) |
| S3 Glacier Deep Archive | $0.00099 | 12–48 hours |

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket YOUR_BUCKET_NAME \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "archive-signed-pdfs",
      "Status": "Enabled",
      "Filter": {"Prefix": "background-checks/"},
      "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]
    }]
  }' \
  --region us-east-2
```

---

## Security notes

- **No secrets in code.** Every API key, webhook secret, and credential is stored in AWS Secrets Manager and fetched at runtime.
- **BoldSign webhook HMAC verification** is enforced before any payload is processed.
- **DynamoDB writes use `ConditionExpression`** for idempotency — duplicate webhook deliveries are safely ignored.
- **S3 Object Lock (Compliance mode)** prevents signed PDFs from being deleted during the retention period, even by an AWS root account.
- **Opt-out links use HMAC tokens** — the URL cannot be guessed or manipulated to opt out another person.

---

## License

MIT
