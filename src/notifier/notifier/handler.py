"""Notifier Lambda handler.

Triggered by EventBridge cron, 30 minutes after the Orchestrator.
Queries the DynamoDB `jobs` table for postings not yet emailed and sends a
single SES email digest to the configured recipient.

Environment variables expected:
    JOBS_TABLE          - DynamoDB table name for job postings
    SES_FROM_ADDRESS    - Verified SES sender email address
    SES_TO_ADDRESS      - Recipient email address
    SES_REGION          - AWS region for SES (defaults to us-east-1)
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote_plus

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = Logger(service="notifier")

dynamodb = boto3.resource("dynamodb")

# Display labels for worker/handler.py's _clearance_tier values, for the
# clearance_tier badge in _build_email_body. Only the three tiers a job can
# actually be kept under appear here — "ambiguous" is rendered via the
# separate clearance_review "CLEARANCE UNCLEAR" badge instead, and "none"
# gets no badge at all.
_CLEARANCE_TIER_LABELS = {
    "top_secret": "TOP SECRET",
    "secret": "SECRET",
    "public_trust": "PUBLIC TRUST",
}


def _query_pending_jobs(table: Any) -> list[dict[str, Any]]:
    """Query the pending-digest-index GSI for jobs not yet emailed.

    The GSI is sparse: `digest_pending` only exists on jobs whose
    `sent_in_digest` is still False, so this Query never has to look at
    already-sent jobs regardless of how large the table grows.

    Args:
        table: boto3 DynamoDB Table resource.

    Returns:
        List of job item dicts.
    """
    response = table.query(
        IndexName="pending-digest-index",
        KeyConditionExpression=Key("digest_pending").eq("pending"),
    )
    return response.get("Items", [])


def _mark_jobs_sent(table: Any, jobs: list[dict[str, Any]]) -> None:
    """Flip sent_in_digest and drop digest_pending for each emailed job.

    Removing digest_pending (rather than just setting it falsy) is what
    drops the item out of the sparse pending-digest-index GSI. Best-effort
    per item: a failure here just means that one job gets re-included in
    the next digest rather than the whole run failing.
    """
    for job in jobs:
        try:
            table.update_item(
                Key={"job_id": job["job_id"]},
                UpdateExpression="REMOVE digest_pending SET sent_in_digest = :true",
                ExpressionAttributeValues={":true": True},
            )
        except ClientError:
            logger.warning("Failed to mark job sent", job_id=job["job_id"])


def _build_email_body(jobs: list[dict[str, Any]]) -> tuple[str, str]:
    """Render plain-text and HTML email bodies from a list of job dicts, grouped by company.

    A job with clearance_review=True (set by the worker for a posting whose
    clearance requirement was ambiguous/unspecified — see worker/handler.py's
    _clearance_decision) is still included, but marked with a review note
    rather than silently guessed at. A job with a known clearance_tier
    (top_secret/secret/public_trust — a posting that was kept because its
    tier's ALLOW_* env var allows it) gets a badge naming that tier, so the
    required level is visible without opening the posting. A job's salary
    (set by the worker's _extract_salary when a posting's description
    contains a pay range) is rendered as a badge/suffix next to the title
    when present; omitted entirely otherwise, since most postings don't
    include one.

    Returns:
        Tuple of (text_body, html_body).
    """
    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        by_company[job["company"]].append(job)

    date_str = datetime.now(UTC).strftime("%B %-d, %Y")
    header = f"Req Aggregator Digest — {len(jobs)} new posting(s), {date_str}"

    text_sections = []
    html_sections = []
    for company in sorted(by_company):
        company_jobs = by_company[company]

        text_lines = []
        html_rows = []
        for job in company_jobs:
            location = job.get("location", "").strip()
            needs_review = bool(job.get("clearance_review"))
            tier_label = _CLEARANCE_TIER_LABELS.get(job.get("clearance_tier", ""))
            salary = job.get("salary", "").strip()
            review_suffix = " [CLEARANCE UNCLEAR - PLEASE VERIFY]" if needs_review else ""
            tier_suffix = f" [{tier_label} CLEARANCE]" if tier_label else ""
            salary_suffix = f" [{salary}]" if salary else ""
            text_lines.append(
                f"  - {job['title']}"
                + (f" ({location})" if location else "")
                + salary_suffix
                + tier_suffix
                + review_suffix
                + f"\n    {job['url']}"
            )
            location_html = (
                f'<p style="margin:4px 0 0;font-size:13px;color:#8a8a9e;">{escape(location)}</p>' if location else ""
            )
            salary_badge = (
                '<span style="display:inline-block;margin-left:8px;padding:2px 8px;border-radius:4px;'
                'background-color:#d4edda;color:#155724;font-size:11px;font-weight:600;">'
                f"{escape(salary)}</span>"
                if salary
                else ""
            )
            tier_badge = (
                '<span style="display:inline-block;margin-left:8px;padding:2px 8px;border-radius:4px;'
                'background-color:#f8d7da;color:#721c24;font-size:11px;font-weight:600;">'
                f"{escape(tier_label)} CLEARANCE</span>"
                if tier_label
                else ""
            )
            review_badge = (
                '<span style="display:inline-block;margin-left:8px;padding:2px 8px;border-radius:4px;'
                'background-color:#fff3cd;color:#856404;font-size:11px;font-weight:600;">'
                "CLEARANCE UNCLEAR</span>"
                if needs_review
                else ""
            )
            html_rows.append(
                f'<div style="padding:12px 0;border-bottom:1px solid #eeeef2;">'
                f'<a href="{escape(job["url"])}" '
                f'style="font-size:15px;font-weight:600;color:#3454d1;text-decoration:none;">'
                f"{escape(job['title'])}</a>{salary_badge}{tier_badge}{review_badge}{location_html}</div>"
            )

        glassdoor_url = f"https://www.glassdoor.com/Search/results.htm?keyword={quote_plus(company)}"

        text_sections.append(f"{company} ({len(company_jobs)}) — Glassdoor: {glassdoor_url}\n" + "\n".join(text_lines))
        html_sections.append(
            f'<div style="margin-top:24px;">'
            f'<p style="margin:0 0 4px;font-size:13px;font-weight:600;color:#6b6b80;'
            f'text-transform:uppercase;letter-spacing:0.05em;">'
            f"{escape(company)} &middot; {len(company_jobs)} "
            f'<a href="{escape(glassdoor_url)}" '
            f'style="text-transform:none;letter-spacing:normal;font-weight:400;color:#3454d1;'
            f'text-decoration:none;">(Glassdoor)</a></p>'
            f"{''.join(html_rows)}</div>"
        )

    text_body = header + "\n\n" + "\n\n".join(text_sections)

    html_body = (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background-color:#f4f4f7;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;\">"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#f4f4f7;padding:24px 0;"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="background-color:#ffffff;border-radius:8px;overflow:hidden;max-width:600px;">'
        '<tr><td style="background-color:#1a1a2e;padding:24px 32px;">'
        f'<p style="margin:0;color:#ffffff;font-size:20px;font-weight:600;">Req Aggregator Digest</p>'
        f'<p style="margin:4px 0 0;color:#a0a0b8;font-size:13px;">'
        f"{len(jobs)} new posting(s) &middot; {date_str}</p></td></tr>"
        f'<tr><td style="padding:8px 32px 32px;">{"".join(html_sections)}</td></tr>'
        "</table></td></tr></table></body></html>"
    )
    return text_body, html_body


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Notifier Lambda.

    Queries recent jobs and sends an SES email digest if any were found.

    Args:
        event: EventBridge scheduled event payload (unused).
        context: Lambda context object (unused).

    Returns:
        A summary dict with the count of jobs emailed.
    """
    jobs_table_name = os.environ["JOBS_TABLE"]
    from_address = os.environ["SES_FROM_ADDRESS"]
    to_address = os.environ["SES_TO_ADDRESS"]
    ses_region = os.environ.get("SES_REGION", "us-east-1")

    table = dynamodb.Table(jobs_table_name)
    jobs = _query_pending_jobs(table)

    if not jobs:
        logger.info("No new jobs found")
        return {"jobs_emailed": 0}

    ses = boto3.client("ses", region_name=ses_region)
    text_body, html_body = _build_email_body(jobs)

    ses.send_email(
        Source=from_address,
        Destination={"ToAddresses": [to_address]},
        Message={
            "Subject": {"Data": f"Req Aggregator: {len(jobs)} new posting(s) found"},
            "Body": {
                "Text": {"Data": text_body},
                "Html": {"Data": html_body},
            },
        },
    )

    _mark_jobs_sent(table, jobs)

    logger.info("Sent digest", job_count=len(jobs), recipient=to_address)
    return {"jobs_emailed": len(jobs)}
