"""Tracker Lambda handler.

Application-tracking HTTP API (API Gateway v2, AWS_IAM-authenticated — no
custom authorizer) for recording per-job application state against postings
the Worker Lambda already wrote to the DynamoDB `jobs` table. This extends
the existing job item schema in place; no separate table.

Routes:
    GET   /jobs          - list/filter jobs (status, discovered_after,
                            discovered_before query params)
    GET   /jobs/{job_id} - fetch a single job
    PATCH /jobs/{job_id} - partially update tracking fields on a job

Environment variables expected:
    JOBS_TABLE - DynamoDB table name for job postings
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.dynamodb.conditions import Attr

logger = Logger(service="tracker")
app = APIGatewayHttpResolver()

dynamodb = boto3.resource("dynamodb")

# The only job-item fields PATCH is allowed to touch — everything else on the
# item (company, title, url, location, discovered_at, clearance_*, etc.) is
# written once by the Worker Lambda and is read-only from this API.
_UPDATABLE_FIELDS = {"date_applied", "salary_range", "source", "status", "response_date", "notes"}
_STATUS_VALUES = {"applied", "interviewing", "rejected", "offer"}


def _table() -> Any:
    return dynamodb.Table(os.environ["JOBS_TABLE"])


@app.get("/jobs")
def list_jobs() -> dict[str, Any]:
    """List jobs, optionally filtered by status and/or a discovered_at date range.

    Query params:
        status: exact match against the job's status field.
        discovered_after / discovered_before: inclusive ISO-8601 bounds on
            discovered_at (the same string sort order works for comparison
            since discovered_at is always written as datetime.now(UTC).isoformat()).

    Filters via FilterExpression, so this always Scans the full table — the
    jobs table has no index over status or a plain discovered_at range (the
    existing pending-digest-index GSI only covers unsent jobs). Paginates
    internally via LastEvaluatedKey so a large table doesn't silently return
    a partial result.

    Returns:
        Dict with "jobs" (list of job items) and "count".
    """
    status = app.current_event.get_query_string_value(name="status")
    discovered_after = app.current_event.get_query_string_value(name="discovered_after")
    discovered_before = app.current_event.get_query_string_value(name="discovered_before")

    filter_expression = None
    for condition in (
        Attr("status").eq(status) if status is not None else None,
        Attr("discovered_at").gte(discovered_after) if discovered_after is not None else None,
        Attr("discovered_at").lte(discovered_before) if discovered_before is not None else None,
    ):
        if condition is None:
            continue
        filter_expression = condition if filter_expression is None else filter_expression & condition

    scan_kwargs: dict[str, Any] = {}
    if filter_expression is not None:
        scan_kwargs["FilterExpression"] = filter_expression

    table = _table()
    items: list[dict[str, Any]] = []
    response = table.scan(**scan_kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(**scan_kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    return {"jobs": items, "count": len(items)}


@app.get("/jobs/<job_id>")
def get_job(job_id: str) -> dict[str, Any]:
    """Fetch a single job by its job_id.

    Returns:
        The job item.

    Raises:
        NotFoundError: job_id doesn't exist.
    """
    item = _table().get_item(Key={"job_id": job_id}).get("Item")
    if item is None:
        raise NotFoundError(f"Job {job_id} not found")
    return item


@app.patch("/jobs/<job_id>")
def update_job(job_id: str) -> dict[str, Any]:
    """Partially update application-tracking fields on a job.

    Accepts a JSON body with one or more of _UPDATABLE_FIELDS; any other key
    is rejected. A "status" value must be one of _STATUS_VALUES.

    Returns:
        The job item after the update (all attributes, not just the changed ones).

    Raises:
        BadRequestError: body isn't a JSON object, is empty, contains an
            unknown field, or has an invalid status value.
        NotFoundError: job_id doesn't exist.
    """
    body = app.current_event.json_body
    if not isinstance(body, dict) or not body:
        raise BadRequestError("Request body must be a non-empty JSON object")

    unknown_fields = set(body) - _UPDATABLE_FIELDS
    if unknown_fields:
        raise BadRequestError(f"Unknown field(s): {', '.join(sorted(unknown_fields))}")

    if "status" in body and body["status"] not in _STATUS_VALUES:
        raise BadRequestError(f"status must be one of: {', '.join(sorted(_STATUS_VALUES))}")

    expr_names = {f"#{field}": field for field in body}
    expr_values = {f":{field}": value for field, value in body.items()}
    update_expression = "SET " + ", ".join(f"#{field} = :{field}" for field in body)

    table = _table()
    try:
        response = table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=update_expression,
            ConditionExpression="attribute_exists(job_id)",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise NotFoundError(f"Job {job_id} not found") from exc

    logger.info("Updated job", job_id=job_id, fields=sorted(body))
    return response["Attributes"]


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Entry point for the Tracker Lambda.

    Args:
        event: API Gateway v2 (HTTP API) proxy event, payload format 2.0.
        context: Lambda context object.

    Returns:
        API Gateway v2 proxy response dict, built by the Powertools resolver.
    """
    return app.resolve(event, context)
