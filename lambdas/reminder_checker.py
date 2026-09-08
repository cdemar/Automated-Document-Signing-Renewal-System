"""Daily reminder checker — runs 8am PST via EventBridge.

Implements the locked-in 3-email flow, exactly:

  Day -14  signer gets the sign-link email (BoldSign envelope is created here)
  Day -7   signer gets a final reminder; owner gets ONE batched email per
           group listing everyone who hasn't signed
  Day 0    owner gets ONE summary per group: X signed / X opted out /
           X never signed, with a table of the never-signed for records

Signing or opting out stops signer emails immediately (those records are
skipped). All thresholds use `days_left <= N` plus a sent-flag rather than
`== N`, so a missed daily run self-heals the next day instead of silently
skipping a person.
"""

import json
import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import boto3

import optout_token
import signing_platform

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
BUCKET = os.environ.get("BUCKET_NAME", "YOUR_BUCKET_NAME")
TABLE_NAME = os.environ.get("TABLE_NAME", "people_forms")
CONFIG_KEY = "config/form_config.json"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
ses = boto3.client("ses", region_name=REGION)


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


def _send_email(config, to_addresses, subject, body_text, body_html=None):
    body = {"Text": {"Data": body_text, "Charset": "UTF-8"}}
    if body_html:
        body["Html"] = {"Data": body_html, "Charset": "UTF-8"}
    ses.send_email(
        Source=config["ses_from"],
        Destination={"ToAddresses": to_addresses},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": body,
        },
    )


def _form_display_name(config, record):
    group_cfg = config["groups"].get(record["group"], {})
    form_cfg = group_cfg.get("forms", {}).get(record["form_type"], {})
    return form_cfg.get("display_name", record["form_type"])


def _ensure_document(config, table, record):
    """Create the BoldSign envelope for this cycle if one doesn't exist yet.

    Returns (document_id, sign_link).
    """
    document_id = record.get("document_id")
    if not document_id:
        group_cfg = config["groups"][record["group"]]
        form_cfg = group_cfg["forms"][record["form_type"]]
        countersign = form_cfg.get("countersign", False)
        document_id = signing_platform.send_from_template(
            template_id=form_cfg["boldsign_template_id"],
            signer_name=record["name"],
            signer_email=record["email"],
            title=form_cfg.get("display_name", record["form_type"]),
            message="Annual renewal — please sign at the link emailed to you.",
            representative_name=group_cfg.get("owner_name") if countersign else None,
            representative_email=group_cfg.get("owner_email") if countersign else None,
            metadata={"person_id": record["person_id"], "form_id": record["form_id"]},
        )
        table.update_item(
            Key={"person_id": record["person_id"], "form_id": record["form_id"]},
            UpdateExpression="SET document_id = :d",
            ExpressionAttributeValues={":d": document_id},
        )
        record["document_id"] = document_id
    sign_link = signing_platform.get_sign_link(document_id, record["email"])
    return document_id, sign_link


def _mark(table, record, today_iso, **flags):
    """Set sent-flags plus the last_reminder/reminder_count dashboard fields."""
    sets = ["last_reminder = :lr", "reminder_count = :rc"]
    values = {
        ":lr": today_iso,
        ":rc": int(record.get("reminder_count") or 0) + 1,
    }
    for i, (key, val) in enumerate(flags.items()):
        sets.append(f"{key} = :f{i}")
        values[f":f{i}"] = val
    table.update_item(
        Key={"person_id": record["person_id"], "form_id": record["form_id"]},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeValues=values,
    )


def _signer_email_body(config, record, sign_link, final):
    deadline = record["renewal_date"]
    opt_url = optout_token.optout_url(
        config["api_base_url"], record["person_id"], record["form_id"]
    )
    group_cfg = config["groups"].get(record["group"], {})
    owner_name = group_cfg.get("owner_name", "Church Administration")
    owner_title = group_cfg.get("owner_title", "")
    sign_off = f"Sincerely,\n{owner_name}" + (f"\n{owner_title}" if owner_title else "")

    if final:
        return (
            f"Dear {record['name']},\n\n"
            f"REMINDER TO COMPLETE\n\n"
            f"Thank you for the time, care, and dedication you give in serving the ministries of "
            f"San Jose Christian Alliance Church. Your faithful service helps us create a welcoming, "
            f"safe, and Christ-centered environment for everyone who walks through our doors.\n\n"
            f"As part of our ongoing commitment to protecting those we serve and complying with "
            f"California legal requirements, we ask all employees and volunteers to complete the "
            f"Annual Live Scan Consent and Waiver Form.\n\n"
            f"Please take a few moments to review, complete, sign, and return this form by {deadline}.\n\n"
            f"Sign here (takes about a minute):\n{sign_link}\n\n"
            f"If you no longer serve in this role and this doesn't apply to you, "
            f"you can opt out here:\n{opt_url}\n\n"
            f"Thank you for your prompt attention to this annual requirement.\n\n"
            f"{sign_off}"
        )
    else:
        return (
            f"Dear {record['name']},\n\n"
            f"Grace and peace to you!\n\n"
            f"Thank you for the time, care, and dedication you give in serving the ministries of "
            f"San Jose Christian Alliance Church. Your faithful service helps us create a welcoming, "
            f"safe, and Christ-centered environment for everyone who walks through our doors.\n\n"
            f"As part of our ongoing commitment to protecting those we serve and complying with "
            f"California legal requirements, we ask all employees and volunteers to complete the "
            f"Annual Live Scan Consent and Waiver Form. This form authorizes San Jose Christian "
            f"Alliance Church (SJCAC) to access and review California Department of Justice (DOJ) "
            f"and Federal Bureau of Investigation (FBI) Criminal Offender Record Information (CORI) "
            f"annually, in accordance with California Penal Code §11105.3. Any information obtained "
            f"through this process will be handled with the highest level of confidentiality and used "
            f"only for employment or volunteer service eligibility, workplace safety, and legal compliance.\n\n"
            f"Please take a few moments to review, complete, sign, and return this form by {deadline}. "
            f"We appreciate your prompt response and your partnership in helping us maintain a safe and "
            f"trustworthy environment for everyone we serve.\n\n"
            f"Sign here (takes about a minute):\n{sign_link}\n\n"
            f"If you no longer serve in this role and this doesn't apply to you, "
            f"you can opt out here:\n{opt_url}\n\n"
            f"If you have any questions regarding the form or the process, please contact me.\n\n"
            f"Thank you for your prompt attention to this annual requirement.\n\n"
            f"{sign_off}"
        )


def _friday_report_text(group, all_records, today_iso):
    today_date = date.fromisoformat(today_iso)
    overdue_repeat = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and r.get("owner_summary_sent")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) <= today_date],
        key=lambda r: r.get("name", "")
    )
    overdue_first = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and not r.get("owner_summary_sent")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) <= today_date],
        key=lambda r: r.get("name", "")
    )
    signed = sorted([r for r in all_records if r.get("signed")], key=lambda r: r.get("name", ""))
    opted = sorted([r for r in all_records if r.get("opted_out") and not r.get("signed")], key=lambda r: r.get("name", ""))
    pending = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) > today_date],
        key=lambda r: r.get("name", "")
    )
    lines = [
        f"Weekly Background Check Report — {today_iso}",
        f"Group: {group}", "",
        f"  {len(signed)} signed  |  {len(opted)} opted out  |  "
        f"{len(overdue_first) + len(overdue_repeat)} overdue  |  {len(pending)} pending", "",
    ]
    if overdue_repeat:
        lines.append("STILL OVERDUE (multiple weeks):")
        for r in overdue_repeat:
            lines.append(f"  - {r.get('name','')} <{r.get('email','')}> — due {r.get('renewal_date','')}")
        lines.append("")
    if overdue_first:
        lines.append("NEW THIS WEEK (first time overdue):")
        for r in overdue_first:
            lines.append(f"  - {r.get('name','')} <{r.get('email','')}> — due {r.get('renewal_date','')}")
        lines.append("")
    if signed:
        lines.append("SIGNED:")
        for r in signed:
            lines.append(f"  - {r.get('name','')} — due {r.get('renewal_date','')}")
        lines.append("")
    if opted:
        lines.append("OPTED OUT:")
        for r in opted:
            lines.append(f"  - {r.get('name','')} <{r.get('email','')}> — due {r.get('renewal_date','')}")
        lines.append("")
    if pending:
        lines.append("PENDING (not yet due):")
        for r in pending:
            lines.append(f"  - {r.get('name','')} — due {r.get('renewal_date','')}")
    if not (overdue_repeat or overdue_first):
        lines.append("No outstanding forms — all caught up!")
    return "\n".join(lines)


def _friday_report_html(group, all_records, today_iso):
    today_date = date.fromisoformat(today_iso)
    overdue_repeat = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and r.get("owner_summary_sent")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) <= today_date],
        key=lambda r: r.get("name", "")
    )
    overdue_first = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and not r.get("owner_summary_sent")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) <= today_date],
        key=lambda r: r.get("name", "")
    )
    signed = sorted([r for r in all_records if r.get("signed")], key=lambda r: r.get("name", ""))
    opted = sorted([r for r in all_records if r.get("opted_out") and not r.get("signed")], key=lambda r: r.get("name", ""))
    pending = sorted(
        [r for r in all_records if not r.get("signed") and not r.get("opted_out")
         and date.fromisoformat(r.get("renewal_date", "9999-12-31")) > today_date],
        key=lambda r: r.get("name", "")
    )

    def stat_tile(label, count, color):
        return (
            f'<td align="center" style="padding:0 16px;">'
            f'<div style="font-size:32px;font-weight:bold;color:{color};">{count}</div>'
            f'<div style="font-size:12px;color:#666;margin-top:4px;text-transform:uppercase;'
            f'letter-spacing:0.05em;">{label}</div></td>'
        )

    def table_section(title, color, header_bg, row_bg, rows_data, show_phone=False):
        if not rows_data:
            return ""
        rows_html = ""
        for r in rows_data:
            phone_cell = (
                f'<td style="padding:8px 12px;font-size:13px;color:#555;">{r.get("phone","—")}</td>'
                if show_phone else ""
            )
            rows_html += (
                f'<tr style="background:{row_bg};">'
                f'<td style="padding:8px 12px;font-size:14px;">{r.get("name","")}</td>'
                f'<td style="padding:8px 12px;font-size:14px;color:#555;">{r.get("email","")}</td>'
                f'<td style="padding:8px 12px;font-size:14px;color:#555;">{r.get("renewal_date","")}</td>'
                f'{phone_cell}</tr>'
            )
        phone_header = (
            '<th style="padding:8px 12px;text-align:left;font-size:12px;">Phone</th>'
            if show_phone else ""
        )
        return (
            f'<h3 style="color:{color};margin:28px 0 8px;font-size:15px;">{title}</h3>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
            f'<tr style="background:{header_bg};">'
            f'<th style="padding:8px 12px;text-align:left;font-size:12px;">Name</th>'
            f'<th style="padding:8px 12px;text-align:left;font-size:12px;">Email</th>'
            f'<th style="padding:8px 12px;text-align:left;font-size:12px;">Due Date</th>'
            f'{phone_header}</tr>{rows_html}</table>'
        )

    sections = ""
    if overdue_repeat:
        sections += table_section(
            f"🔴 Still Overdue — Multiple Weeks ({len(overdue_repeat)})",
            "#dc2626", "#fca5a5", "#FEE2E2", overdue_repeat, show_phone=True,
        )
    if overdue_first:
        sections += table_section(
            f"🟡 New This Week — First Time Overdue ({len(overdue_first)})",
            "#d97706", "#fde68a", "#FEF3C7", overdue_first, show_phone=True,
        )
    if signed:
        sections += table_section(
            f"✅ Signed ({len(signed)})", "#16a34a", "#bbf7d0", "#F0FDF4", signed,
        )
    if opted:
        sections += table_section(
            f"⛔ Opted Out ({len(opted)})", "#6b7280", "#e5e7eb", "#F9FAFB", opted,
        )
    if pending:
        sections += table_section(
            f"🕐 Pending — Not Yet Due ({len(pending)})", "#2563eb", "#bfdbfe", "#EFF6FF", pending,
        )
    if not sections:
        sections = (
            '<p style="color:#16a34a;font-size:16px;font-weight:bold;">'
            '✅ All caught up — no outstanding forms this week.</p>'
        )

    n_overdue = len(overdue_first) + len(overdue_repeat)
    overdue_color = "#dc2626" if n_overdue > 0 else "#16a34a"
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="680" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;padding:40px;max-width:680px;">
        <tr><td>
          <h2 style="margin:0 0 4px;color:#1a1a1a;font-size:20px;">Weekly Background Check Report</h2>
          <p style="margin:0 0 28px;color:#666;font-size:14px;">Week of {today_iso} &mdash; {group}</p>
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#f8f9fa;border-radius:8px;padding:20px 0;margin-bottom:8px;">
            <tr>
              {stat_tile("Signed", len(signed), "#16a34a")}
              {stat_tile("Opted Out", len(opted), "#6b7280")}
              {stat_tile("Overdue", n_overdue, overdue_color)}
              {stat_tile("Pending", len(pending), "#2563eb")}
            </tr>
          </table>
          {sections}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _signer_email_html(config, record, sign_link, opt_url, final):
    deadline = record["renewal_date"]
    group_cfg = config["groups"].get(record["group"], {})
    owner_name = group_cfg.get("owner_name", "Church Administration")
    owner_title = group_cfg.get("owner_title", "")
    sign_off = f"Sincerely,<br>{owner_name}" + (f"<br>{owner_title}" if owner_title else "")

    if final:
        body_html = f"""
          <p><strong>REMINDER TO COMPLETE</strong></p>
          <p>Thank you for the time, care, and dedication you give in serving the ministries of
          San Jose Christian Alliance Church. Your faithful service helps us create a welcoming,
          safe, and Christ-centered environment for everyone who walks through our doors.</p>
          <p>As part of our ongoing commitment to protecting those we serve and complying with
          California legal requirements, we ask all employees and volunteers to complete the
          Annual Live Scan Consent and Waiver Form.</p>
          <p>Please take a few moments to review, complete, sign, and return this form by {deadline}.</p>"""
    else:
        body_html = f"""
          <p>Grace and peace to you!</p>
          <p>Thank you for the time, care, and dedication you give in serving the ministries of
          San Jose Christian Alliance Church. Your faithful service helps us create a welcoming,
          safe, and Christ-centered environment for everyone who walks through our doors.</p>
          <p>As part of our ongoing commitment to protecting those we serve and complying with
          California legal requirements, we ask all employees and volunteers to complete the
          Annual Live Scan Consent and Waiver Form. This form authorizes San Jose Christian
          Alliance Church (SJCAC) to access and review California Department of Justice (DOJ)
          and Federal Bureau of Investigation (FBI) Criminal Offender Record Information (CORI)
          annually, in accordance with California Penal Code §11105.3. Any information obtained
          through this process will be handled with the highest level of confidentiality and used
          only for employment or volunteer service eligibility, workplace safety, and legal compliance.</p>
          <p>Please take a few moments to review, complete, sign, and return this form by {deadline}.
          We appreciate your prompt response and your partnership in helping us maintain a safe and
          trustworthy environment for everyone we serve.</p>
          <p>If you have any questions regarding the form or the process, please contact me.</p>"""

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;padding:40px;max-width:600px;">
        <tr><td style="color:#1a1a1a;font-size:15px;line-height:1.7;">
          <p>Dear {record['name']},</p>
          {body_html}
          <p style="margin:28px 0;">
            <a href="{sign_link}"
               style="background:#2563eb;color:#ffffff;padding:14px 28px;text-decoration:none;
                      border-radius:6px;font-size:15px;font-weight:bold;display:inline-block;">
              Sign Now &rarr;
            </a>
          </p>
          <p style="font-size:13px;color:#666;">
            If you no longer serve in this role and this doesn&#39;t apply to you, you can
            <a href="{opt_url}" style="color:#2563eb;">opt out here</a>.
          </p>
          <p>Thank you for your prompt attention to this annual requirement.</p>
          <p>{sign_off}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def lambda_handler(event, context):
    config = _load_config()
    table = dynamodb.Table(TABLE_NAME)
    today = datetime.now(LOCAL_TZ).date()
    today_iso = today.isoformat()

    records = _scan_all(table)
    day7_groups = {}    # group -> {"unsigned": [...], "newly_notified": [...]}
    sent_initial = sent_final = 0

    for record in records:
        renewal = record.get("renewal_date")
        if not renewal:
            continue
        try:
            days_left = (date.fromisoformat(renewal) - today).days
        except ValueError:
            logger.warning("Bad renewal_date %r for %s — skipping", renewal, record["person_id"])
            continue

        group = record.get("group", "unknown")

        # Signer emails: signing or opting out stops these immediately.
        if record.get("signed") or record.get("opted_out"):
            continue

        if days_left <= 0:
            continue  # past deadline — covered by the day-0 summary above

        if days_left <= 7:
            entry = day7_groups.setdefault(group, {"unsigned": [], "newly_notified": []})
            entry["unsigned"].append(record)
            if not record.get("owner_notified"):
                entry["newly_notified"].append(record)
            if not record.get("reminder_email_sent"):
                try:
                    _, sign_link = _ensure_document(config, table, record)
                    opt_url = optout_token.optout_url(
                        config["api_base_url"], record["person_id"], record["form_id"]
                    )
                    _send_email(
                        config, [record["email"]],
                        f"Final reminder: {_form_display_name(config, record)} due {renewal}",
                        _signer_email_body(config, record, sign_link, final=True),
                        _signer_email_html(config, record, sign_link, opt_url, final=True),
                    )
                    _mark(table, record, today_iso,
                          reminder_email_sent=True, initial_email_sent=True)
                    sent_final += 1
                except Exception:
                    logger.exception("Final reminder failed for %s", record["person_id"])
        elif days_left <= 14:
            if not record.get("initial_email_sent"):
                try:
                    _, sign_link = _ensure_document(config, table, record)
                    opt_url = optout_token.optout_url(
                        config["api_base_url"], record["person_id"], record["form_id"]
                    )
                    _send_email(
                        config, [record["email"]],
                        f"Please sign: {_form_display_name(config, record)} due {renewal}",
                        _signer_email_body(config, record, sign_link, final=False),
                        _signer_email_html(config, record, sign_link, opt_url, final=False),
                    )
                    _mark(table, record, today_iso, initial_email_sent=True)
                    sent_initial += 1
                except Exception:
                    logger.exception("Initial email failed for %s", record["person_id"])

    # Day -7 owner emails: one per group, only when someone newly crossed
    # the 7-day mark, always listing the full unsigned picture.
    for group, entry in day7_groups.items():
        if not entry["newly_notified"]:
            continue
        owner = config["groups"].get(group, {}).get("owner_email")
        if not owner:
            logger.error("No owner_email configured for group %s", group)
            continue
        lines = [
            f"  - {r['name']} <{r['email']}> — due {r['renewal_date']}"
            for r in sorted(entry["unsigned"], key=lambda r: r["name"])
        ]
        _send_email(
            config, [owner],
            f"[{group}] {len(entry['unsigned'])} people have not signed yet",
            "These people have a form due within 7 days and have not signed:\n\n"
            + "\n".join(lines)
            + "\n\nThey have each received a final reminder today. "
              "No action needed unless you want to follow up personally.",
        )
        for r in entry["newly_notified"]:
            table.update_item(
                Key={"person_id": r["person_id"], "form_id": r["form_id"]},
                UpdateExpression="SET owner_notified = :t",
                ExpressionAttributeValues={":t": True},
            )

    # Friday weekly report: one per group, always sent every Friday.
    # 🟡 Yellow = first time overdue (owner_summary_sent not yet set)
    # 🔴 Red    = still overdue after appearing in a prior Friday report
    friday_reports_sent = 0
    if today.weekday() == 4:  # Friday
        groups_in_db = {}
        for record in records:
            g = record.get("group", "unknown")
            if record.get("renewal_date"):
                groups_in_db.setdefault(g, []).append(record)

        for group, group_records in groups_in_db.items():
            owner = config["groups"].get(group, {}).get("owner_email")
            if not owner:
                logger.error("No owner_email configured for group %s", group)
                continue
            try:
                _send_email(
                    config, [owner],
                    f"[{group}] Weekly background check report — {today_iso}",
                    _friday_report_text(group, group_records, today_iso),
                    _friday_report_html(group, group_records, today_iso),
                )
                # Mark first-time overdue records so next Friday they show as red
                today_date = today
                for r in group_records:
                    if (not r.get("signed") and not r.get("opted_out")
                            and not r.get("owner_summary_sent")
                            and r.get("renewal_date")):
                        try:
                            if date.fromisoformat(r["renewal_date"]) <= today_date:
                                table.update_item(
                                    Key={"person_id": r["person_id"], "form_id": r["form_id"]},
                                    UpdateExpression="SET owner_summary_sent = :t",
                                    ExpressionAttributeValues={":t": True},
                                )
                        except ValueError:
                            pass
                friday_reports_sent += 1
            except Exception:
                logger.exception("Friday report failed for group %s", group)

    summary = {
        "initial_emails": sent_initial,
        "final_reminders": sent_final,
        "owner_list_emails": sum(1 for e in day7_groups.values() if e["newly_notified"]),
        "friday_reports": friday_reports_sent,
    }
    logger.info("Reminder run complete: %s", json.dumps(summary))
    return summary
