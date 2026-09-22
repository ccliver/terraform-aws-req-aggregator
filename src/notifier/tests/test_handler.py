"""Tests for the Notifier Lambda handler."""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from notifier.handler import _build_email_body, handler

REGION = "us-east-1"
FROM_ADDRESS = "noreply@example.com"
TO_ADDRESS = "me@example.com"


@pytest.fixture()
def aws_resources(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="test-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "digest_pending", "AttributeType": "S"},
                {"AttributeName": "discovered_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "pending-digest-index",
                    "KeySchema": [
                        {"AttributeName": "digest_pending", "KeyType": "HASH"},
                        {"AttributeName": "discovered_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        ses = boto3.client("ses", region_name=REGION)
        ses.verify_email_identity(EmailAddress=FROM_ADDRESS)
        ses.verify_email_identity(EmailAddress=TO_ADDRESS)

        monkeypatch.setenv("JOBS_TABLE", "test-jobs")
        monkeypatch.setenv("SES_FROM_ADDRESS", FROM_ADDRESS)
        monkeypatch.setenv("SES_TO_ADDRESS", TO_ADDRESS)
        monkeypatch.setenv("SES_REGION", REGION)

        yield {"table": table, "ses": ses}


def _job(job_id: str, title: str, sent: bool = False) -> dict:
    item = {
        "job_id": job_id,
        "company": "Acme",
        "title": title,
        "url": f"https://acme.com/{job_id}",
        "location": "Remote",
        "discovered_at": datetime.now(UTC).isoformat(),
        "sent_in_digest": sent,
    }
    if not sent:
        item["digest_pending"] = "pending"
    return item


def test_build_email_body_contains_job_info() -> None:
    """Email body should include job title, company, and URL."""
    jobs = [
        {
            "title": "SWE",
            "company": "Acme",
            "url": "https://acme.com/1",
            "location": "Remote",
        }
    ]
    text, html = _build_email_body(jobs)

    assert "SWE" in text
    assert "Acme" in text
    assert "https://acme.com/1" in text
    assert "SWE" in html
    assert "https://acme.com/1" in html


def test_build_email_body_includes_location() -> None:
    """Email body should include each job's location."""
    jobs = [{"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": "Austin, TX"}]
    text, html = _build_email_body(jobs)

    assert "Austin, TX" in text
    assert "Austin, TX" in html


def test_build_email_body_groups_by_company() -> None:
    """Email body should group multiple jobs from the same company together."""
    jobs = [
        {"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"},
        {"title": "SRE", "company": "Acme", "url": "https://acme.com/2", "location": "Remote"},
        {"title": "DevOps Engineer", "company": "Beta Corp", "url": "https://beta.com/1", "location": "Remote"},
    ]
    text, html = _build_email_body(jobs)

    assert "Acme (2)" in text
    assert "Beta Corp (1)" in text
    assert "Acme &middot; 2" in html
    assert "Beta Corp &middot; 1" in html


def test_build_email_body_escapes_html_special_characters() -> None:
    """Email HTML body should escape special characters in job titles."""
    jobs = [{"title": "R&D Engineer <Platform>", "company": "Acme", "url": "https://acme.com/1", "location": ""}]
    _, html = _build_email_body(jobs)

    assert "R&amp;D Engineer &lt;Platform&gt;" in html
    assert "<Platform>" not in html


def test_build_email_body_flags_ambiguous_clearance_jobs() -> None:
    """Email body should mark a job flagged clearance_review=True for manual review."""
    jobs = [
        {
            "title": "Cloud Engineer",
            "company": "Acme",
            "url": "https://acme.com/1",
            "location": "Remote",
            "clearance_review": True,
        }
    ]
    text, html = _build_email_body(jobs)

    assert "[CLEARANCE UNCLEAR - PLEASE VERIFY]" in text
    assert "CLEARANCE UNCLEAR" in html


def test_build_email_body_omits_clearance_flag_when_not_set() -> None:
    """Email body should not mention clearance review for a normal job."""
    jobs = [{"title": "Cloud Engineer", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"}]
    text, html = _build_email_body(jobs)

    assert "CLEARANCE" not in text
    assert "CLEARANCE" not in html


def test_build_email_body_includes_salary_when_present() -> None:
    """Email body should render a job's salary when the worker found one."""
    jobs = [
        {
            "title": "Cloud Engineer",
            "company": "Acme",
            "url": "https://acme.com/1",
            "location": "Remote",
            "salary": "$120,000 - $150,000",
        }
    ]
    text, html = _build_email_body(jobs)

    assert "[$120,000 - $150,000]" in text
    assert "$120,000 - $150,000</span>" in html


def test_build_email_body_omits_salary_when_not_set() -> None:
    """Email body should not mention salary for a job with no salary field."""
    jobs = [{"title": "Cloud Engineer", "company": "Acme", "url": "https://acme.com/1", "location": "Remote"}]
    text, html = _build_email_body(jobs)

    assert "[$" not in text
    assert "#d4edda" not in html


def test_build_email_body_omits_location_line_when_blank() -> None:
    """Email body should not render an empty location line when location is missing."""
    jobs = [{"title": "SWE", "company": "Acme", "url": "https://acme.com/1", "location": ""}]
    text, html = _build_email_body(jobs)

    assert "()" not in text
    assert "color:#8a8a9e" not in html


def test_handler_no_jobs_skips_email(aws_resources: dict, lambda_context) -> None:
    """handler() should not send an email when no recent jobs are found."""
    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 0
    send_stats = aws_resources["ses"].get_send_statistics()
    delivery_attempts = sum(p["DeliveryAttempts"] for p in send_stats["SendDataPoints"])
    assert delivery_attempts == 0


def test_handler_sends_email_when_jobs_found(aws_resources: dict, lambda_context) -> None:
    """handler() should send one SES email when pending jobs exist."""
    aws_resources["table"].put_item(Item=_job("job-1", "SWE"))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 1
    send_stats = aws_resources["ses"].get_send_statistics()
    assert send_stats["SendDataPoints"] != []


def test_handler_ignores_already_sent_jobs(aws_resources: dict, lambda_context) -> None:
    """handler() should not re-email a job already marked sent_in_digest."""
    aws_resources["table"].put_item(Item=_job("job-old", "SWE", sent=True))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 0


def test_handler_emails_all_pending_jobs(aws_resources: dict, lambda_context) -> None:
    """handler() should include all pending jobs in one email."""
    aws_resources["table"].put_item(Item=_job("job-1", "SWE"))
    aws_resources["table"].put_item(Item=_job("job-2", "SRE"))

    result = handler({}, lambda_context)

    assert result["jobs_emailed"] == 2


def test_handler_marks_jobs_sent_after_email(aws_resources: dict, lambda_context) -> None:
    """handler() should flip sent_in_digest and drop digest_pending after a successful send."""
    aws_resources["table"].put_item(Item=_job("job-1", "SWE"))

    handler({}, lambda_context)

    item = aws_resources["table"].get_item(Key={"job_id": "job-1"})["Item"]
    assert item["sent_in_digest"] is True
    assert "digest_pending" not in item
