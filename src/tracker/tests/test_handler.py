"""Tests for the Tracker Lambda handler."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from tracker.handler import handler

REGION = "us-east-1"


@pytest.fixture()
def aws_resources(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName="test-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        monkeypatch.setenv("JOBS_TABLE", "test-jobs")

        yield {"table": table}


def _job(job_id: str, **overrides) -> dict:
    item = {
        "job_id": job_id,
        "company": "Acme",
        "title": "SWE",
        "url": f"https://acme.com/{job_id}",
        "location": "Remote",
        "discovered_at": datetime.now(UTC).isoformat(),
        "sent_in_digest": True,
    }
    item.update(overrides)
    return item


def _event(method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> dict:
    """Build a minimal API Gateway v2 (HTTP API, payload format 2.0) proxy event."""
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "headers": {"content-type": "application/json"},
        "queryStringParameters": query or None,
        "requestContext": {
            "http": {"method": method, "path": path},
            "requestId": "test-request-id",
            "routeKey": f"{method} {path}",
            "stage": "$default",
        },
        "isBase64Encoded": False,
        "body": json.dumps(body) if body is not None else None,
    }


def test_list_jobs_returns_all_jobs(aws_resources: dict, lambda_context) -> None:
    """GET /jobs with no filters should return every job in the table."""
    aws_resources["table"].put_item(Item=_job("job-1"))
    aws_resources["table"].put_item(Item=_job("job-2"))

    result = handler(_event("GET", "/jobs"), lambda_context)

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["count"] == 2


def test_list_jobs_filters_by_status(aws_resources: dict, lambda_context) -> None:
    """GET /jobs?status=applied should only return jobs with that status."""
    aws_resources["table"].put_item(Item=_job("job-1", status="applied"))
    aws_resources["table"].put_item(Item=_job("job-2", status="rejected"))

    result = handler(_event("GET", "/jobs", query={"status": "applied"}), lambda_context)

    body = json.loads(result["body"])
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "job-1"


def test_list_jobs_filters_by_discovered_at_range(aws_resources: dict, lambda_context) -> None:
    """GET /jobs?discovered_after=...&discovered_before=... should bound by discovered_at."""
    aws_resources["table"].put_item(Item=_job("job-old", discovered_at="2020-01-01T00:00:00+00:00"))
    aws_resources["table"].put_item(Item=_job("job-mid", discovered_at="2024-06-01T00:00:00+00:00"))
    aws_resources["table"].put_item(Item=_job("job-new", discovered_at="2030-01-01T00:00:00+00:00"))

    result = handler(
        _event(
            "GET",
            "/jobs",
            query={"discovered_after": "2023-01-01T00:00:00+00:00", "discovered_before": "2025-01-01T00:00:00+00:00"},
        ),
        lambda_context,
    )

    body = json.loads(result["body"])
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "job-mid"


def test_get_job_returns_matching_job(aws_resources: dict, lambda_context) -> None:
    """GET /jobs/{job_id} should return the matching job."""
    aws_resources["table"].put_item(Item=_job("job-1", title="SRE"))

    result = handler(_event("GET", "/jobs/job-1"), lambda_context)

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["title"] == "SRE"


def test_get_job_returns_404_for_unknown_job(aws_resources: dict, lambda_context) -> None:
    """GET /jobs/{job_id} should return 404 for a job_id that doesn't exist."""
    result = handler(_event("GET", "/jobs/does-not-exist"), lambda_context)

    assert result["statusCode"] == 404


def test_patch_job_updates_allowed_fields(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should update the given fields and return the full item."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(
        _event("PATCH", "/jobs/job-1", body={"status": "applied", "notes": "Referred by a friend"}),
        lambda_context,
    )

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["status"] == "applied"
    assert body["notes"] == "Referred by a friend"

    item = aws_resources["table"].get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "applied"


def test_patch_job_allows_partial_update(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should not require every updatable field to be present."""
    aws_resources["table"].put_item(Item=_job("job-1", status="applied"))

    result = handler(_event("PATCH", "/jobs/job-1", body={"notes": "Following up"}), lambda_context)

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["notes"] == "Following up"
    assert body["status"] == "applied"


def test_patch_job_rejects_unknown_field(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should reject a field outside the updatable set."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("PATCH", "/jobs/job-1", body={"title": "New Title"}), lambda_context)

    assert result["statusCode"] == 400


def test_patch_job_rejects_invalid_status(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should reject a status value outside the enum."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("PATCH", "/jobs/job-1", body={"status": "ghosted"}), lambda_context)

    assert result["statusCode"] == 400


def test_patch_job_rejects_empty_body(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should reject a request with no fields to update."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("PATCH", "/jobs/job-1", body={}), lambda_context)

    assert result["statusCode"] == 400


def test_patch_job_returns_404_for_unknown_job(aws_resources: dict, lambda_context) -> None:
    """PATCH /jobs/{job_id} should return 404 for a job_id that doesn't exist."""
    result = handler(_event("PATCH", "/jobs/does-not-exist", body={"status": "applied"}), lambda_context)

    assert result["statusCode"] == 404


@pytest.mark.parametrize("status", ["not_applied", "applied", "interviewing", "rejected", "offer"])
def test_patch_job_accepts_every_valid_status(aws_resources: dict, lambda_context, status: str) -> None:
    """PATCH /jobs/{job_id} should accept every value in the status enum."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("PATCH", "/jobs/job-1", body={"status": status}), lambda_context)

    assert result["statusCode"] == 200


def test_get_job_defaults_missing_status_to_not_applied(aws_resources: dict, lambda_context) -> None:
    """GET /jobs/{job_id} should report status="not_applied" for a job the Worker wrote with no status attribute."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("GET", "/jobs/job-1"), lambda_context)

    body = json.loads(result["body"])
    assert body["status"] == "not_applied"
    # The DynamoDB item itself is untouched — this is a response-shaping default, not a write.
    item = aws_resources["table"].get_item(Key={"job_id": "job-1"})["Item"]
    assert "status" not in item


def test_list_jobs_defaults_missing_status_to_not_applied(aws_resources: dict, lambda_context) -> None:
    """GET /jobs should report status="not_applied" for every job with no status attribute."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("GET", "/jobs"), lambda_context)

    body = json.loads(result["body"])
    assert body["jobs"][0]["status"] == "not_applied"


def test_list_jobs_filters_by_not_applied_includes_untouched_jobs(aws_resources: dict, lambda_context) -> None:
    """GET /jobs?status=not_applied should match jobs with no status attribute, not just an explicit "not_applied"."""
    aws_resources["table"].put_item(Item=_job("job-untouched"))
    aws_resources["table"].put_item(Item=_job("job-explicit", status="not_applied"))
    aws_resources["table"].put_item(Item=_job("job-applied", status="applied"))

    result = handler(_event("GET", "/jobs", query={"status": "not_applied"}), lambda_context)

    body = json.loads(result["body"])
    assert body["count"] == 2
    assert {j["job_id"] for j in body["jobs"]} == {"job-untouched", "job-explicit"}


def test_list_jobs_filters_by_applied_excludes_untouched_jobs(aws_resources: dict, lambda_context) -> None:
    """GET /jobs?status=applied should not match jobs with no status attribute at all."""
    aws_resources["table"].put_item(Item=_job("job-untouched"))
    aws_resources["table"].put_item(Item=_job("job-applied", status="applied"))

    result = handler(_event("GET", "/jobs", query={"status": "applied"}), lambda_context)

    body = json.loads(result["body"])
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "job-applied"


def test_patch_job_defaults_missing_status_in_response_when_not_touched(aws_resources: dict, lambda_context) -> None:
    """PATCH updating only notes on an untouched job should still report status="not_applied" in the response."""
    aws_resources["table"].put_item(Item=_job("job-1"))

    result = handler(_event("PATCH", "/jobs/job-1", body={"notes": "Following up"}), lambda_context)

    body = json.loads(result["body"])
    assert result["statusCode"] == 200
    assert body["status"] == "not_applied"
    # Still not actually written to the item — only the response defaults it.
    item = aws_resources["table"].get_item(Key={"job_id": "job-1"})["Item"]
    assert "status" not in item
