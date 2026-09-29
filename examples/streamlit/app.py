"""Streamlit UI for the req-aggregator application-tracking API.

Talks directly to the AWS_IAM-authorized tracking API (API Gateway HTTP API
+ tracker Lambda) that this module creates when enable_tracking_api = true —
see the "Application Tracking" section of the repo README. Every request is
SigV4-signed with botocore using boto3's default AWS credential chain
(env vars, ~/.aws/*, SSO, etc.) — there's no separate login/auth UI, no API
key, and nothing but the signed HTTP request leaves your machine.

Run:
    cd examples/streamlit
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import pandas as pd
import requests
import streamlit as st
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import BotoCoreError, ClientError

st.set_page_config(page_title="Req Aggregator — Job Tracker", page_icon="🧭", layout="wide")

_STATUS_VALUES = ["not_applied", "applied", "interviewing", "rejected", "offer"]
# Monokai accents, matching .streamlit/config.toml — applied to table rows
# via a low-opacity (hex alpha suffix) tint of the status's color.
# "not_applied" deliberately has no entry: it's the default/no-action-taken
# state (the API reports it for a job the Worker wrote but nobody's touched
# yet), so leaving it untinted keeps visual weight on rows you've acted on.
_STATUS_COLORS = {
    "applied": "#66D9EF",
    "interviewing": "#E6DB74",
    "rejected": "#F92672",
    "offer": "#A6E22E",
}
_EDITABLE_COLUMNS = ["status", "date_applied", "response_date", "notes"]
_DATE_COLUMNS = ["discovered_at", "date_applied", "response_date"]
_TEXT_COLUMNS = ["company", "title", "location", "url", "salary", "status", "notes"]
_DISPLAY_COLUMNS = ["company", "title", "location", "discovered_at", "url", "salary", *_EDITABLE_COLUMNS]


def _secret(key: str) -> str:
    """Read a key from .streamlit/secrets.toml, tolerating a missing/absent file entirely."""
    try:
        return st.secrets.get(key, "")
    except Exception:
        return ""


@st.cache_resource(show_spinner=False)
def _session() -> boto3.Session:
    """The local AWS credential chain (env vars, ~/.aws/*, SSO, etc.) — no separate login flow."""
    return boto3.Session()


def _signed_request(
    method: str,
    base_url: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> requests.Response:
    """SigV4-sign a request (service execute-api) and send it.

    Every route on the tracking API requires AWS_IAM authorization — no API
    key or bearer token — so each call needs a real SigV4 signature computed
    from the caller's own AWS credentials, exactly like any other AWS API
    call. Query params are passed to AWSRequest itself (not pre-appended to
    the URL) so botocore signs the same query string it ends up sending;
    signing a URL that doesn't yet include the params it will be sent with
    produces a signature AWS rejects.
    """
    session = _session()
    region = session.region_name
    if not region:
        raise RuntimeError(
            "No AWS region configured. Set AWS_REGION/AWS_DEFAULT_REGION, or `region` in your "
            "AWS profile — it must match the region the tracking API was deployed into."
        )
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials found. Configure them (e.g. `aws configure` or `aws sso login`) "
            "for an identity with execute-api:Invoke on the tracking API."
        )

    url = base_url.rstrip("/") + path
    body = json.dumps(json_body) if json_body is not None else None
    request = AWSRequest(method=method, url=url, data=body, params=params or {})
    if body is not None:
        request.headers["Content-Type"] = "application/json"
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    prepared = request.prepare()
    return requests.request(method, prepared.url, headers=dict(prepared.headers), data=prepared.body, timeout=30)


def _error_message(resp: requests.Response) -> str:
    """Pull the {"message": ...} the tracker's error responses carry, falling back to raw text."""
    try:
        return resp.json().get("message", resp.text)
    except ValueError:
        return resp.text


def list_jobs(base_url: str, *, discovered_after: str | None, discovered_before: str | None) -> list[dict[str, Any]]:
    """Fetch jobs matching the (optional) discovered_at range — status/text filtering happen client-side.

    Status is deliberately not passed to the API here: filtering it locally
    (see _apply_filters) means the summary tiles below can show accurate
    counts across every status at once, and switching between them is
    instant with no extra request, instead of narrowing what's fetched and
    then not knowing how many jobs exist outside that narrowed set.
    """
    params = {}
    if discovered_after:
        params["discovered_after"] = discovered_after
    if discovered_before:
        params["discovered_before"] = discovered_before
    resp = _signed_request("GET", base_url, "/jobs", params=params)
    if not resp.ok:
        raise RuntimeError(f"GET /jobs failed ({resp.status_code}): {_error_message(resp)}")
    return resp.json()["jobs"]


def patch_job(base_url: str, job_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    resp = _signed_request("PATCH", base_url, f"/jobs/{job_id}", json_body=fields)
    if not resp.ok:
        raise RuntimeError(f"PATCH /jobs/{job_id} failed ({resp.status_code}): {_error_message(resp)}")
    return resp.json()


def _caller_identity() -> str | None:
    """Best-effort 'who am I' for the sidebar — sts:GetCallerIdentity needs no IAM grant of its own."""
    try:
        return _session().client("sts").get_caller_identity()["Arn"]
    except (BotoCoreError, ClientError):
        return None


def _jobs_to_dataframe(jobs: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalise a list of job dicts (heterogeneous keys — most tracking fields are set-once-touched) into a fixed-column table."""
    df = pd.DataFrame(jobs)
    for col in ["job_id", *_DISPLAY_COLUMNS]:
        if col not in df.columns:
            df[col] = None
    # Sort on the full discovered_at timestamp (correctly chronological as a
    # plain string sort) *before* truncating it to a date below — otherwise
    # same-day postings would lose their relative order.
    df = df.sort_values("discovered_at", ascending=False)
    for col in _DATE_COLUMNS:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    for col in _TEXT_COLUMNS:
        df[col] = df[col].fillna("")
    return df[["job_id", *_DISPLAY_COLUMNS]].reset_index(drop=True)


def _apply_filters(df: pd.DataFrame, *, status: str, search: str) -> pd.DataFrame:
    """Client-side status + free-text (company/title) filtering — no extra API call either way."""
    if status != "(any)":
        df = df[df["status"] == status]
    if search:
        needle = search.strip().lower()
        df = df[df["company"].str.lower().str.contains(needle) | df["title"].str.lower().str.contains(needle)]
    return df


def _style_by_status(df: pd.DataFrame) -> Any:
    """Tint every row by its status color. Only applies to data_editor's non-editable columns (see caller)."""

    def _row_style(row: pd.Series) -> list[str]:
        color = _STATUS_COLORS.get(row["status"])
        return [f"background-color: {color}22" if color else ""] * len(row)

    return df.style.apply(_row_style, axis=1)


def _cell_to_patch_value(column: str, value: Any) -> str | None:
    """Normalise one edited cell into the string PATCH expects, or None if it shouldn't be sent.

    "status" can only be sent as one of the API's enum values — a blank cell
    is skipped rather than sent, since the API rejects anything outside the
    enum and there's no way to "unset" status anyway. Every other field
    accepts any string (including ""), so a cell cleared back to blank is
    sent as "" — the closest this SET-only API gets to clearing a value.
    """
    # pd.isna() (not a bare `is None`/float-NaN check) is required here: a cleared
    # DateColumn cell comes back as pd.NaT, not None or float nan.
    is_blank = value == "" if isinstance(value, str) else (value is None or pd.isna(value))
    if column == "status":
        return value if not is_blank and value in _STATUS_VALUES else None
    if column in ("date_applied", "response_date"):
        return "" if is_blank else value.isoformat()
    return "" if is_blank else str(value)


def _row_patch(original: pd.Series, edited: pd.Series) -> dict[str, Any]:
    """Diff one row's editable columns; only columns that actually changed end up in the PATCH body."""
    patch = {}
    for column in _EDITABLE_COLUMNS:
        new_value = _cell_to_patch_value(column, edited[column])
        old_value = _cell_to_patch_value(column, original[column])
        if new_value is not None and new_value != old_value:
            patch[column] = new_value
    return patch


def _set_status_filter(value: str) -> None:
    """Point both the sidebar selectbox and the URL at `value`, so a tile click is bookmarkable/shareable.

    Can't assign st.session_state["status_filter"] directly here: this runs
    from a tile button handler *after* the sidebar selectbox bound to that
    same key has already been instantiated this script run, and Streamlit
    forbids reassigning a widget's key post-instantiation. Stashing it under
    a different key and applying it at the top of the next run (before the
    selectbox is created) sidesteps that.
    """
    st.session_state["status_filter_pending"] = value
    if value == "(any)":
        st.query_params.pop("status", None)
    else:
        st.query_params["status"] = value


@st.dialog("Edit job", width="large")
def _edit_job_dialog(base_url: str, jobs_df: pd.DataFrame, job_id: str) -> None:
    """A focused, single-job editor — every field visible at once, no horizontal scrolling."""
    row = jobs_df.loc[jobs_df["job_id"] == job_id].iloc[0]
    st.subheader(f"{row['title']} — {row['company']}")
    st.caption(row["location"] or "—")
    if row["salary"]:
        st.caption(f"💰 {row['salary']} (detected from the posting)")
    if row["url"]:
        st.markdown(f"[Open posting ↗]({row['url']})")

    status = st.selectbox(
        "Status", _STATUS_VALUES, index=_STATUS_VALUES.index(row["status"]) if row["status"] in _STATUS_VALUES else 0
    )
    col1, col2 = st.columns(2)
    date_applied = col1.date_input("Date applied", value=row["date_applied"] if pd.notna(row["date_applied"]) else None)
    response_date = col2.date_input(
        "Response date", value=row["response_date"] if pd.notna(row["response_date"]) else None
    )
    notes = st.text_area("Notes", value=row["notes"], height=160)

    if st.button("Save", type="primary"):
        edited = row.copy()
        edited["status"], edited["date_applied"], edited["response_date"] = status, date_applied, response_date
        edited["notes"] = notes
        patch = _row_patch(row, edited)
        if not patch:
            st.info("No changes to save.")
            return
        try:
            patch_job(base_url, job_id, patch)
        except (RuntimeError, requests.RequestException) as exc:
            st.error(str(exc))
            return
        st.session_state.pop("jobs_df", None)
        st.session_state.pop("jobs_editor", None)
        st.rerun()


# --- Sidebar: connection + filters -------------------------------------------

# Must run before the "status_filter" selectbox below is instantiated — see
# the docstring on _set_status_filter for why this can't happen in-place.
if "status_filter_pending" in st.session_state:
    st.session_state["status_filter"] = st.session_state.pop("status_filter_pending")

with st.sidebar:
    st.header("Connection")
    default_api_url = os.environ.get("API_URL") or _secret("API_URL")
    base_url = st.text_input(
        "API invoke URL",
        key="api_base_url",
        value=default_api_url,
        placeholder="https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com",
        help="The `tracking_api_invoke_url` Terraform output. Set via the API_URL env var or "
        ".streamlit/secrets.toml to avoid pasting it in each time — see README.md.",
    )
    identity = _caller_identity()
    st.caption(f"Signed in as `{identity}`" if identity else "No AWS credentials found in the local environment.")
    st.caption(
        "Requests are SigV4-signed locally with your AWS credentials and sent straight to API "
        "Gateway — no login UI, no server in between. The identity above needs "
        "`execute-api:Invoke` on this API's ARN."
    )

    st.header("Filters")
    st.session_state.setdefault("status_filter", st.query_params.get("status") or "(any)")
    status_filter = st.selectbox("Status", ["(any)", *_STATUS_VALUES], key="status_filter")
    search_text = st.text_input("Search company / title", placeholder="e.g. platform")
    discovered_after = st.date_input("Discovered after", value=None)
    discovered_before = st.date_input("Discovered before", value=None)
    search = st.button("Search", use_container_width=True)
    st.caption("Status and search filter instantly with no extra request; date range needs Search.")

st.title("🧭 Req Aggregator — Job Tracker")

if not base_url:
    st.info("Enter the tracking API's invoke URL in the sidebar to get started.")
    st.stop()

if search or "jobs_df" not in st.session_state:
    try:
        jobs = list_jobs(
            base_url,
            discovered_after=discovered_after.isoformat() if discovered_after else None,
            discovered_before=discovered_before.isoformat() if discovered_before else None,
        )
    except (RuntimeError, requests.RequestException) as exc:
        st.error(str(exc))
        st.stop()
    st.session_state["jobs_df"] = _jobs_to_dataframe(jobs)
    st.session_state.pop("jobs_editor", None)

jobs_df: pd.DataFrame = st.session_state["jobs_df"]

# Tiles always reflect the full fetched set, regardless of the active status
# filter, so switching between them never shows a stale/zeroed-out count.
tile_specs = [
    ("Total", "(any)"),
    ("Not Applied", "not_applied"),
    ("Applied", "applied"),
    ("Interviewing", "interviewing"),
    ("Offer", "offer"),
    ("Rejected", "rejected"),
]
tile_cols = st.columns(len(tile_specs))
for col, (label, value) in zip(tile_cols, tile_specs, strict=True):
    count = len(jobs_df) if value == "(any)" else int((jobs_df["status"] == value).sum())
    with col:
        st.metric(label, count)
        if st.button("View →", key=f"tile_{value}", use_container_width=True):
            _set_status_filter(value)
            st.rerun()

visible_df = _apply_filters(jobs_df, status=status_filter, search=search_text)

top_cols = st.columns([5, 1])
top_cols[0].caption(
    f"{len(visible_df)} of {len(jobs_df)} job(s) shown, colored by status. Click ✏️ to edit a job "
    "in a focused view, or change its status directly in the table, then Save changes."
)
top_cols[1].download_button(
    "Export CSV",
    data=visible_df.drop(columns=["job_id"]).to_csv(index=False),
    file_name="jobs.csv",
    mime="text/csv",
    use_container_width=True,
)

edited_df = st.data_editor(
    _style_by_status(visible_df.assign(edit="✏️")),
    key="jobs_editor",
    use_container_width=True,
    hide_index=True,
    num_rows="fixed",
    column_order=["edit", *_DISPLAY_COLUMNS],
    column_config={
        # Only "status" stays inline-editable — every other tracking field
        # moves to edit-via-dialog-only. This is what lets the status color
        # tint apply to (almost) the whole row: st.data_editor only applies
        # pandas.Styler backgrounds to non-editable columns, so an inline-
        # editable cell can't be tinted. See the "Edit job" dialog for
        # date_applied/response_date/notes.
        "edit": st.column_config.ButtonColumn("", width="small", key="edit_click"),
        "company": st.column_config.TextColumn("Company", disabled=True),
        "title": st.column_config.TextColumn("Title", disabled=True),
        "location": st.column_config.TextColumn("Location", disabled=True),
        "discovered_at": st.column_config.DateColumn("Discovered", disabled=True),
        "url": st.column_config.LinkColumn("Posting", display_text="Open ↗", disabled=True),
        "salary": st.column_config.TextColumn("Salary", disabled=True, help="Auto-detected from the posting."),
        "status": st.column_config.SelectboxColumn("Status", options=_STATUS_VALUES),
        "date_applied": st.column_config.DateColumn("Date applied", disabled=True),
        "response_date": st.column_config.DateColumn("Response date", disabled=True),
        "notes": st.column_config.TextColumn("Notes", disabled=True),
    },
)

# ButtonColumn resets this to None on the rerun after the one that set it, so
# there's no risk of the dialog re-popping on a later, unrelated rerun.
edit_click = st.session_state.get("edit_click")
if edit_click is not None:
    _edit_job_dialog(base_url, jobs_df, visible_df.iloc[edit_click.row]["job_id"])

if st.button("Save changes", type="primary"):
    errors = []
    saved = 0
    with st.spinner("Saving..."):
        for i in edited_df.index:
            patch = _row_patch(jobs_df.loc[i], edited_df.loc[i])
            if not patch:
                continue
            job_id = jobs_df.loc[i, "job_id"]
            try:
                patch_job(base_url, job_id, patch)
                saved += 1
            except (RuntimeError, requests.RequestException) as exc:
                errors.append(f"{jobs_df.loc[i, 'title']} ({job_id}): {exc}")

    if saved:
        st.success(f"Saved {saved} job(s).")
    if errors:
        st.error("Some updates failed:\n\n" + "\n\n".join(errors))
    if saved:
        st.session_state.pop("jobs_df", None)
        st.session_state.pop("jobs_editor", None)
        st.rerun()
