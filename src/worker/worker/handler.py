"""Worker Lambda handler.

Triggered by SQS. Each message contains a company name, careers URL, and
optional ATS type. Dispatches to the appropriate ATS handler to fetch and
parse job listings, applies a keyword filter, then writes new postings to
the DynamoDB `jobs` table. Deduplication is achieved by hashing
company+title+url as the DynamoDB partition key (job_id).

ATS backends:
    greenhouse - JSON API
    lever      - JSON API
    workday    - Unofficial JSON API (cxs)
    builtin    - Built In (builtin.com) search results page. Aggregates
                 postings across many employers, so each returned job carries
                 its own "company" key instead of relying on the SQS
                 message's company_name. Jobs from companies already tracked
                 directly elsewhere in companies.json are skipped.
    oracle     - Oracle Fusion Cloud Recruiting's public REST API
                 (recruitingCEJobRequisitions) — the same endpoint the
                 career site's own search page calls, unauthenticated.

All environment variables below are required — this app has no config
defaults of its own (12-factor: config lives in the environment, supplied
by Terraform; see variables.tf for the actual default values used at
deploy time). Tests set them in src/worker/tests/conftest.py.

Environment variables expected:
    JOBS_TABLE      - DynamoDB table name for job postings
    COMPANIES_TABLE - DynamoDB table name for tracked companies (used by the
                       builtin ATS backend to skip already-tracked companies)
    ALLOW_PUBLIC_TRUST      - "true"/"false"; whether to keep postings
                       requiring a Public Trust clearance (see
                       _clearance_decision).
    ALLOW_SECRET_CLEARANCE  - "true"/"false"; whether to keep postings
                       requiring a Secret-tier clearance (Secret, DoD Secret,
                       Interim Secret, or the DOE-equivalent L clearance) —
                       no polygraph or friends/family interviews required.
    ALLOW_TOP_SECRET_CLEARANCE - "true"/"false"; whether to keep postings
                       requiring a Top-Secret-tier or above clearance (Top
                       Secret, TS/SCI, a polygraph, a Special Access Program,
                       or the DOE-equivalent Q clearance).
                       A generic/unspecified clearance mention with no level
                       given is never excluded outright — it's kept and
                       flagged (clearance_review=True on the job dict, and
                       written to DynamoDB) for manual review in the
                       notifier digest instead, unless every tier above is
                       already allowed. When every tier is allowed, the
                       Workday/Built In fetchers also skip the extra
                       per-posting detail-page request that exists only to
                       feed this check — see _clearance_screening_needed.
    LOCATION          - Comma-separated location substrings to additionally
                         keep (OR'd together) for every ATS backend except
                         builtin; blank disables it (remote-only)
    WORK_TYPE         - Work-type keyword to keep for every ATS backend except
                         builtin: "remote", "hybrid", "office", "any", or any
                         other literal substring to match
    BUILTIN_LOCATION  - Same as LOCATION (also comma-separated), but for the
                         builtin ATS backend only — independent setting
    BUILTIN_WORK_TYPE - Same as WORK_TYPE, but for the builtin ATS backend
                         only — independent setting
    TITLE_KEYWORDS         - Comma-separated title substrings (OR'd together,
                              case-insensitive); a job title must match at
                              least one to be kept at all, for every ATS
                              backend; also drives one paginated Workday
                              search per entry
    EXCLUDE_TITLE_KEYWORDS - Comma-separated title substrings (OR'd together,
                              case-insensitive); a title matching any of these
                              is dropped even if it also matched
                              TITLE_KEYWORDS
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import boto3
import requests
from aws_lambda_powertools import Logger
from bs4 import BeautifulSoup
from bs4.element import Tag

logger = Logger(service="worker")

dynamodb = boto3.resource("dynamodb")

_WORKDAY_URL_RE = re.compile(r"^https://([^./]+)\.(wd\d+)\.myworkdayjobs\.com/([^/?#]+)")
_WORKDAY_PAGE_SIZE = 20
_WORKDAY_MAX_JOBS_PER_KEYWORD = 1000

_BUILTIN_BASE_URL = "https://builtin.com"
_BUILTIN_MAX_PAGES = 15

_ORACLE_URL_RE = re.compile(r"^https://([^/]+)/hcmUI/CandidateExperience/([^/]+)/sites/([^/?#]+)")
_ORACLE_PAGE_SIZE = 20
_ORACLE_MAX_JOBS_PER_KEYWORD = 1000

_WORK_TYPE_KEYWORDS = {
    "remote": ["remote", "distributed", "anywhere"],
    "hybrid": ["hybrid"],
    "office": ["in-office", "in office", "on-site", "onsite"],
}


def _title_keywords() -> list[str]:
    """Configured TITLE_KEYWORDS, lowercased and stripped.

    A job title must match at least one of these (case-insensitive
    substring) to be kept at all — see _title_looks_relevant /
    _filter_relevant_jobs. Also drives one paginated Workday search per
    entry in _fetch_workday_jobs. Read fresh from the env on every call
    (not cached at import time), same pattern as _work_type_matches, so
    tests can monkeypatch.setenv per-test.
    """
    return [kw.strip().lower() for kw in os.environ["TITLE_KEYWORDS"].split(",") if kw.strip()]


def _exclude_title_keywords() -> list[str]:
    """Configured EXCLUDE_TITLE_KEYWORDS, lowercased and stripped.

    A title matching any of these (case-insensitive substring) is dropped
    even if it also matched TITLE_KEYWORDS — management/leadership roles,
    not IC roles. Read fresh on every call, same reasoning as
    _title_keywords.
    """
    return [kw.strip().lower() for kw in os.environ["EXCLUDE_TITLE_KEYWORDS"].split(",") if kw.strip()]


# Top-Secret-and-above keywords — the tier that requires a polygraph and
# friends/family interviews. Checked before _SECRET_KEYWORDS since some
# phrases overlap as substrings (e.g. "top secret clearance" also contains
# "secret clearance"); check order makes the higher tier win.
_TOP_SECRET_KEYWORDS = [
    "top secret",
    "ts/sci",
    "ts sci",
    "polygraph",
    "full scope poly",
    "ci poly",
    "sci clearance",
    "special access program",
    "sap clearance",
    "q clearance",  # DOE's Top-Secret-equivalent
]

# Secret-tier keywords — no polygraph or friends/family interviews required.
_SECRET_KEYWORDS = [
    "secret clearance",
    "dod secret",
    "interim secret",
    "l clearance",  # DOE's Secret-equivalent
]

# Unspecified/generic clearance mentions with no level given — the actual
# tier can't be determined from text alone, so these are never excluded
# outright; see _clearance_decision.
_GENERIC_CLEARANCE_KEYWORDS = [
    "security clearance",
    "active clearance",
    "clearance required",
    "clearance sponsorship",
    "must possess a clearance",
    "must obtain a clearance",
    "eligible for a clearance",
    "clearable",
]

# Explicit negations checked before _GENERIC_CLEARANCE_KEYWORDS, since e.g.
# "no clearance required" would otherwise substring-match "clearance required".
_NO_CLEARANCE_PHRASES = [
    "no clearance required",
    "no security clearance required",
    "clearance not required",
    "clearance is not required",
    "does not require a clearance",
    "does not require a security clearance",
]

# Standard US employment-law notice boilerplate that would otherwise
# false-positive match a clearance keyword despite having nothing to do with
# government clearance — e.g. the required EPPA notice mentions "polygraph"
# and is present on nearly every US company's careers page.
_CLEARANCE_FALSE_POSITIVE_PHRASES = [
    "employee polygraph protection act",
]


def _allow_public_trust() -> bool:
    return os.environ["ALLOW_PUBLIC_TRUST"].lower() == "true"


def _allow_secret_clearance() -> bool:
    return os.environ["ALLOW_SECRET_CLEARANCE"].lower() == "true"


def _allow_top_secret_clearance() -> bool:
    return os.environ["ALLOW_TOP_SECRET_CLEARANCE"].lower() == "true"


def _clearance_screening_needed() -> bool:
    """Whether a posting's clearance requirement could still affect the outcome.

    False only when every tier (Public Trust, Secret, Top Secret) is
    allowed — in that case no clearance mention could exclude a posting, and
    an ambiguous mention has nothing left to resolve either. The Workday and
    Built In fetchers no longer skip their extra per-posting detail-page
    request in that case (they used to, back when clearance screening was
    its only consumer) — that request also feeds _extract_salary now, so it
    always runs for a relevant-titled posting.
    """
    return not (_allow_public_trust() and _allow_secret_clearance() and _allow_top_secret_clearance())


def _clearance_tier(text: str) -> str:
    """Classify text's clearance requirement into a tier.

    Returns "top_secret", "secret", "public_trust", "ambiguous" (a
    generic/unspecified mention with no level given), or "none" (no
    clearance mentioned, or an explicit "no clearance required" negation).
    Hyphens are normalised to spaces before matching — "Top-Secret" is the
    grammatically standard hyphenation as a compound modifier ("a top-secret
    clearance") and is common in postings, but keyword phrases below are
    written space-separated; without this, a hyphenated "top-secret
    clearance" fails every _TOP_SECRET_KEYWORDS entry and instead
    substring-matches _SECRET_KEYWORDS's "secret clearance" (the text right
    after the hyphen), misclassifying a Top Secret requirement as merely
    Secret — the wrong direction to get wrong. Known false-positive
    boilerplate (e.g. the EPPA notice, which mentions "polygraph" but has
    nothing to do with government clearance) is stripped before matching.
    Checked highest tier first so overlapping substrings (e.g. "top secret
    clearance" also containing "secret clearance") resolve to the higher
    tier.
    """
    text_lower = text.lower().replace("-", " ")
    for phrase in _CLEARANCE_FALSE_POSITIVE_PHRASES:
        text_lower = text_lower.replace(phrase, "")
    if any(kw in text_lower for kw in _TOP_SECRET_KEYWORDS):
        return "top_secret"
    if any(kw in text_lower for kw in _SECRET_KEYWORDS):
        return "secret"
    if "public trust" in text_lower:
        return "public_trust"
    if any(phrase in text_lower for phrase in _NO_CLEARANCE_PHRASES):
        return "none"
    if any(kw in text_lower for kw in _GENERIC_CLEARANCE_KEYWORDS):
        return "ambiguous"
    return "none"


def _clearance_decision(text: str) -> tuple[bool, bool]:
    """Decide whether text's clearance requirement should exclude the posting.

    Returns (excluded, needs_review):
      - "top_secret"/"secret"/"public_trust": excluded is the inverse of
        that tier's ALLOW_* env var; the tier is known, so needs_review is
        always False.
      - "ambiguous": never excluded outright — a generic mention with no
        level given could be any tier, and guessing risks hiding a posting
        the user would've been fine with. Flagged for manual review
        (needs_review=True) via the notifier digest instead, unless every
        tier is already allowed (_clearance_screening_needed() is False), in
        which case a review would be pointless.
      - "none": kept, no review needed.
    """
    tier = _clearance_tier(text)
    if tier == "top_secret":
        return not _allow_top_secret_clearance(), False
    if tier == "secret":
        return not _allow_secret_clearance(), False
    if tier == "public_trust":
        return not _allow_public_trust(), False
    if tier == "ambiguous":
        return False, _clearance_screening_needed()
    return False, False


# Countries, business regions, and common offshore/nearshore tech-hub cities
# that indicate a non-US location. Deliberately excludes ambiguous names that
# collide with US places (e.g. "Georgia" the country vs. the US state,
# "Jordan" the country vs. a common name) — those are left included by
# default rather than risk hiding a real US posting. Matched with word
# boundaries (see _NON_US_LOCATION_RE) so short entries like "uk" don't
# false-positive inside words like "Milwaukee".
_NON_US_LOCATION_KEYWORDS = [
    # Business regions
    "emea",
    "apac",
    "latam",
    # Countries
    "india",
    "canada",
    "united kingdom",
    "uk",
    "england",
    "scotland",
    "wales",
    "ireland",
    "germany",
    "france",
    "spain",
    "italy",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "ukraine",
    "israel",
    "australia",
    "new zealand",
    "singapore",
    "japan",
    "china",
    "hong kong",
    "taiwan",
    "korea",
    "philippines",
    "vietnam",
    "thailand",
    "malaysia",
    "indonesia",
    "pakistan",
    "bangladesh",
    "mexico",
    "brazil",
    "argentina",
    "chile",
    "colombia",
    "peru",
    "costa rica",
    "south africa",
    "nigeria",
    "kenya",
    "egypt",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "turkey",
    "switzerland",
    "austria",
    "belgium",
    "denmark",
    "sweden",
    "norway",
    "finland",
    "czech republic",
    "hungary",
    "greece",
    "russia",
    # Common offshore/nearshore tech-hub cities (no country name attached)
    "bangalore",
    "bengaluru",
    "hyderabad",
    "pune",
    "mumbai",
    "gurgaon",
    "gurugram",
    "noida",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "london",
    "dublin",
    "manchester",
    "edinburgh",
    "belfast",
    "berlin",
    "munich",
    "frankfurt",
    "hamburg",
    "paris",
    "madrid",
    "barcelona",
    "milan",
    "amsterdam",
    "warsaw",
    "krakow",
    "prague",
    "budapest",
    "bucharest",
    "tel aviv",
    "herzliya",
    "tokyo",
    "seoul",
    "shanghai",
    "beijing",
    "shenzhen",
    "manila",
    "ho chi minh",
    "hanoi",
    "bangkok",
    "jakarta",
    "kuala lumpur",
    "sydney",
    "melbourne",
    "auckland",
    "wellington",
    "sao paulo",
    "são paulo",
    "mexico city",
    "bogota",
    "buenos aires",
    "cape town",
    "johannesburg",
    "lagos",
    "nairobi",
    "cairo",
    "heredia",
]

_NON_US_LOCATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _NON_US_LOCATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_non_us_location(location: str) -> bool:
    """Check whether a location string indicates a non-US location.

    Defaults to False (kept) for ambiguous or unhelpful strings like a bare
    "Remote" or "N Locations" — a false negative (a non-US job slipping
    through) is preferable to a false positive (hiding a real US job over an
    incidental keyword match).
    """
    if not location:
        return False
    return bool(_NON_US_LOCATION_RE.search(location))


def _title_looks_relevant(title: str) -> bool:
    """Cheap title-only pre-check mirroring _filter_relevant_jobs's keyword logic.

    Used by fetchers that can fetch a full job description at the cost of an
    extra request per posting (e.g. Workday), to avoid paying that cost for
    postings that would be dropped by _filter_relevant_jobs anyway.
    """
    title_lower = title.lower()
    if not any(kw in title_lower for kw in _title_keywords()):
        return False
    return not any(kw in title_lower for kw in _exclude_title_keywords())


def _make_job_id(company: str, title: str, url: str) -> str:
    """Derive a stable deduplication key from company, title, and URL."""
    raw = f"{company}|{title}|{url}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _filter_relevant_jobs(jobs: list[dict[str, Any]], company: str) -> list[dict[str, Any]]:
    """Drop jobs whose title doesn't match a target-role keyword, or matches an excluded one.

    Performs case-insensitive substring matching against TITLE_KEYWORDS,
    then drops any of those matches whose title also hits EXCLUDE_TITLE_KEYWORDS
    (management/leadership roles), has a location indicating a non-US
    posting, or (for every backend except "builtin") doesn't match the
    configured LOCATION/WORK_TYPE. Built In jobs are exempt from that last
    check since they're already filtered by their own independent
    BUILTIN_LOCATION/BUILTIN_WORK_TYPE config in _fetch_builtin_jobs —
    detected here via the per-job "company" key, which only the builtin
    backend sets. Clearance filtering isn't done here: every fetcher (_fetch_
    greenhouse_jobs/_fetch_lever_jobs/_fetch_workday_jobs/_fetch_builtin_jobs)
    already applies _clearance_decision itself, using the fullest text it has
    available (title alone for Lever, title+description for the others) —
    re-checking title-only text here could contradict a decision the fetcher
    already made using the full description (e.g. a title alone might look
    like an unspecified/ambiguous clearance mention that the full description
    actually resolved to an allowed Public Trust posting). Logs extracted vs.
    matched counts so the keyword lists can be tuned.

    Args:
        jobs: Raw list of job dicts with at least a "title" key.
        company: Company name used for structured log context.

    Returns:
        Subset of jobs whose title matched a target keyword, hit no exclude
        keyword, whose location isn't non-US, and (unless from the builtin
        backend) matches the configured work type.
    """
    matched = [j for j in jobs if any(kw in j.get("title", "").lower() for kw in _title_keywords())]
    filtered = [j for j in matched if not any(kw in j["title"].lower() for kw in _exclude_title_keywords())]
    us_only = [j for j in filtered if not _is_non_us_location(j.get("location", ""))]
    work_type_matched = [j for j in us_only if "company" in j or _location_matches(j.get("location", ""))]
    logger.info(
        "Job filter complete",
        company=company,
        extracted=len(jobs),
        matched=len(matched),
        excluded=len(matched) - len(filtered),
        non_us_excluded=len(filtered) - len(us_only),
        work_type_excluded=len(us_only) - len(work_type_matched),
        dropped=len(jobs) - len(work_type_matched),
    )
    return work_type_matched


_SALARY_RANGE_RE = re.compile(r"\$?\s*\d{2,3},?\d{3}\s*(?:-|–|—|to)\s*\$?\s*\d{2,3},?\d{3}")


def _extract_salary(text: str) -> str | None:
    """Look for a salary range in job description text, e.g. "$120,000 - $150,000".

    Matches two 5-6 digit numbers (each optionally comma-separated and
    dollar-prefixed) joined by a dash or "to", rather than anchoring on a
    keyword like "salary" — that keyword isn't reliably near the numbers in
    practice, while the paired-number shape itself is a strong enough signal
    that unrelated figures (employee counts, revenue) rarely produce this
    exact adjacency by coincidence.
    """
    match = _SALARY_RANGE_RE.search(text)
    return match.group(0).strip() if match else None


def _fetch_greenhouse_jobs(careers_url: str) -> list[dict[str, Any]]:
    """Fetch job listings from a Greenhouse JSON API endpoint.

    Requests full job descriptions (content=true) at no extra cost — the
    Greenhouse list endpoint includes them in the same response — so
    excluded-tier postings can be dropped even when the title alone doesn't
    say so. See _clearance_decision.

    Args:
        careers_url: Greenhouse board API URL (already returns JSON).

    Returns:
        Normalised list of job dicts with title, url, location keys (plus
        clearance_review=True for jobs with an ambiguous clearance mention,
        and salary when a pay range is found in the description — see
        _extract_salary).
    """
    try:
        resp = requests.get(careers_url, params={"content": "true"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Greenhouse fetch failed", url=careers_url, error=str(exc))
        return []

    try:
        data = resp.json()
    except requests.exceptions.JSONDecodeError:
        logger.warning(
            "Greenhouse response is not JSON — careers_url must be the board API endpoint "
            "(e.g. https://boards-api.greenhouse.io/v1/boards/{slug}/jobs), not the human-facing page",
            url=careers_url,
        )
        return []

    jobs = []
    clearance_skipped = 0
    for posting in data.get("jobs", []):
        title = posting.get("title", "")
        content = posting.get("content", "")
        excluded, needs_review = _clearance_decision(f"{title} {content}")
        if excluded:
            clearance_skipped += 1
            continue
        job = {
            "title": title,
            "url": posting.get("absolute_url", careers_url),
            "location": posting.get("location", {}).get("name", ""),
        }
        if needs_review:
            job["clearance_review"] = True
        salary = _extract_salary(content)
        if salary:
            job["salary"] = salary
        jobs.append(job)
    logger.info("Greenhouse jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
    return jobs


def _fetch_lever_jobs(careers_url: str) -> list[dict[str, Any]]:
    """Fetch job listings from a Lever JSON API endpoint.

    The list response carries no job description, so the clearance check
    (_clearance_decision) is title-only here, unlike Greenhouse/Workday/Built
    In which also see the full description.

    Args:
        careers_url: Lever postings API URL (already returns JSON).

    Returns:
        Normalised list of job dicts with title, url, location keys (plus
        clearance_review=True for jobs with an ambiguous clearance mention).
    """
    try:
        resp = requests.get(careers_url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Lever fetch failed", url=careers_url, error=str(exc))
        return []

    try:
        data = resp.json()
    except requests.exceptions.JSONDecodeError:
        logger.warning(
            "Lever response is not JSON — careers_url must be the postings API endpoint "
            "(e.g. https://api.lever.co/v0/postings/{slug}), not the human-facing page",
            url=careers_url,
        )
        return []

    jobs = []
    clearance_skipped = 0
    for posting in data:
        title = posting.get("text", "")
        excluded, needs_review = _clearance_decision(title)
        if excluded:
            clearance_skipped += 1
            continue
        job = {
            "title": title,
            "url": posting.get("hostedUrl", careers_url),
            "location": posting.get("categories", {}).get("location", ""),
        }
        if needs_review:
            job["clearance_review"] = True
        jobs.append(job)
    logger.info("Lever jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
    return jobs


def _fetch_workday_job_description(tenant: str, wd: str, site: str, external_path: str) -> str:
    """Fetch a single Workday posting's full description via its detail endpoint.

    Returns "" on any failure — callers fall back to title-only clearance
    checking in that case, rather than dropping the job outright over a
    transient error.
    """
    detail_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
    try:
        resp = requests.get(detail_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, requests.exceptions.JSONDecodeError) as exc:
        logger.warning("Workday job detail fetch failed", url=detail_url, error=str(exc))
        return ""
    return data.get("jobPostingInfo", {}).get("jobDescription", "")


def _fetch_workday_jobs(careers_url: str) -> list[dict[str, Any]]:
    """Fetch job listings from a Workday-hosted careers site via its unofficial JSON API.

    Parses the tenant/site from a myworkdayjobs.com careers URL, then issues
    one paginated search per TITLE_KEYWORDS entry (via the `searchText`
    param) instead of paginating the company's entire board unfiltered.
    Company board sizes vary enormously — a few hundred postings for a
    startup vs. 17,000+ for a national retail chain with a posting per store
    — but Workday's search narrows results server-side, so the keyword-
    scoped subset stays a manageable size regardless of company size (e.g.
    empirically, CVS's ~17,700 total postings narrow to under 300 for any
    single one of these keywords). Workday's search is a fuzzy full-text
    match, not an exact substring one (e.g. searching "platform" surfaces
    unrelated titles too), so every result is still re-checked with the
    exact _title_looks_relevant filter before being kept — this only saves
    us from scanning thousands of irrelevant postings to find the relevant
    ones. The same posting can surface under multiple keywords, so seen_paths
    dedupes across searches to avoid double-processing (and double-fetching
    descriptions for) the same posting. For postings whose title already
    looks relevant, a follow-up request fetches the full description — both
    to catch clearance requirements that aren't mentioned in the title, and
    to look for a salary range (see _extract_salary). Always fetched now;
    previously skipped when _clearance_screening_needed() was False, back
    when clearance was the description's only consumer.

    Args:
        careers_url: Careers URL of the form
            https://{tenant}.wd{N}.myworkdayjobs.com/{site}.

    Returns:
        Normalised list of job dicts with title, url, location keys (plus
        clearance_review=True for an ambiguous clearance mention, and salary
        when a pay range is found in the description).
    """
    match = _WORKDAY_URL_RE.match(careers_url)
    if not match:
        logger.warning("Not a parseable myworkdayjobs.com URL", url=careers_url)
        return []
    tenant, wd, site = match.groups()
    base_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    api_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    jobs = []
    clearance_skipped = 0
    seen_paths: set[str] = set()

    for keyword in _title_keywords():
        offset = 0
        while offset < _WORKDAY_MAX_JOBS_PER_KEYWORD:
            try:
                resp = requests.post(
                    api_url,
                    json={"limit": _WORKDAY_PAGE_SIZE, "offset": offset, "searchText": keyword},
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("Workday fetch failed", url=api_url, keyword=keyword, error=str(exc))
                break

            try:
                data = resp.json()
            except requests.exceptions.JSONDecodeError:
                logger.warning("Workday response is not JSON", url=api_url, keyword=keyword)
                break

            postings = data.get("jobPostings", [])
            if not postings:
                break

            for posting in postings:
                external_path = posting.get("externalPath", "")
                if external_path in seen_paths:
                    continue
                title = posting.get("title", "")
                if not _title_looks_relevant(title):
                    continue
                seen_paths.add(external_path)
                description = _fetch_workday_job_description(tenant, wd, site, external_path)
                excluded, needs_review = _clearance_decision(f"{title} {description}")
                if excluded:
                    clearance_skipped += 1
                    continue
                job = {
                    "title": title,
                    "url": base_url + external_path,
                    "location": posting.get("locationsText", ""),
                }
                if needs_review:
                    job["clearance_review"] = True
                salary = _extract_salary(description)
                if salary:
                    job["salary"] = salary
                jobs.append(job)

            offset += _WORKDAY_PAGE_SIZE
            if offset >= data.get("total", 0):
                break

    logger.info("Workday jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
    return jobs


def _get_known_company_names() -> set[str]:
    """Return the lowercased names of companies already tracked in COMPANIES_TABLE."""
    table = dynamodb.Table(os.environ["COMPANIES_TABLE"])
    items = table.scan(ProjectionExpression="company_name").get("Items", [])
    return {item["company_name"].lower() for item in items}


def _is_known_company(company: str, known_companies: set[str]) -> bool:
    """Check whether a Built In company name matches an already-tracked company.

    Uses substring containment (not just exact match) since Built In's display
    name for a company often differs slightly from companies.json (e.g. "CACI"
    vs "CACI International", "Coinbase Global, Inc." vs "Coinbase").
    """
    company_lower = company.lower()
    return any(known in company_lower or company_lower in known for known in known_companies)


def _work_type_matches(location: str, location_env_var: str, work_type_env_var: str) -> bool:
    """Shared implementation behind _location_matches and _builtin_location_matches.

    A job is kept if its location contains any of the configured target
    locations as a whole word (location_env_var: comma-separated, OR'd
    together — e.g. "VA,Virginia,DC" to catch both abbreviated and
    spelled-out state names across ATS backends that format locations
    differently), or its location indicates the configured work type (or the
    work type env var is "any", or its value isn't a recognised keyword, in
    which case it's matched literally as a substring too). Target locations
    are matched with word boundaries, not raw substring containment — e.g.
    "VA" must not match inside "Sunnyvale, CA" — mirroring how
    _NON_US_LOCATION_RE avoids "uk" matching inside "Milwaukee". If the work
    type is "any" and no target locations are configured, the whole check is
    disabled and every job passes, blank location included. A blank location
    otherwise passes only when the configured work type is "remote" — many
    ATS listings leave location empty specifically for fully-remote roles,
    so that's treated as a match rather than as missing data. For any other
    configured work type (hybrid/office/a literal string), blank location
    gives no evidence either way and fails the match.
    """
    target_locations = [loc.strip().lower() for loc in os.environ[location_env_var].split(",") if loc.strip()]
    work_type = os.environ[work_type_env_var].lower()

    if not target_locations and work_type == "any":
        return True

    if not location:
        return work_type == "remote"
    location_lower = location.lower()

    if target_locations:
        target_re = re.compile(r"\b(" + "|".join(re.escape(t) for t in target_locations) + r")\b")
        if target_re.search(location_lower):
            return True
    if work_type == "any":
        return True
    keywords = _WORK_TYPE_KEYWORDS.get(work_type, [work_type])
    return any(kw in location_lower for kw in keywords)


def _location_matches(location: str) -> bool:
    """Check a job's location against the configured LOCATION / WORK_TYPE env vars.

    Applies to every ATS backend except "builtin", which has its own
    independent BUILTIN_LOCATION / BUILTIN_WORK_TYPE config (see
    _builtin_location_matches) — kept separate because the curated company
    list includes companies chosen for proximity to a specific future
    location, so a hybrid/on-site preference there shouldn't be governed by
    the same "remote only" default that makes sense for Built In's broad
    discovery search. See _work_type_matches for the shared matching rules.
    """
    return _work_type_matches(location, "LOCATION", "WORK_TYPE")


def _builtin_location_matches(location: str) -> bool:
    """Check a Built In job's location against the configured target location or work type.

    Controlled by the BUILTIN_LOCATION / BUILTIN_WORK_TYPE env vars,
    independent of the LOCATION / WORK_TYPE env vars used by every other
    backend (see _location_matches). See _work_type_matches for the shared
    matching rules.
    """
    return _work_type_matches(location, "BUILTIN_LOCATION", "BUILTIN_WORK_TYPE")


def _builtin_card_text_by_icon(card: Tag, icon_class: str) -> str:
    """Extract the text sibling next to a Font Awesome icon within a Built In job card."""
    icon = card.select_one(f".{icon_class}")
    if not icon:
        return ""
    parent = icon.find_parent("div")
    sibling = parent.find_next_sibling() if parent else None
    return sibling.get_text(strip=True) if sibling else ""


def _fetch_builtin_job_description(url: str) -> str:
    """Fetch a single Built In job's detail page and return its cleaned text.

    The full description is present in the server-rendered page — no special
    container selector needed. Returns "" on any failure — callers fall back
    to title-only clearance checking in that case, rather than dropping the
    job outright over a transient error.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Built In job detail fetch failed", url=url, error=str(exc))
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def _fetch_builtin_jobs(careers_url: str) -> list[dict[str, Any]]:
    """Fetch job listings from a Built In (builtin.com) search results page.

    The search page is server-rendered, so a plain GET is enough — no
    headless browser needed. Paginates via the `page` query param until a
    page returns no job cards.
    Built In aggregates postings across many employers, so each job dict
    carries its own "company" key; jobs from companies already tracked
    directly elsewhere in companies.json are skipped (they're covered, often
    more completely, by their own direct fetch). The search results don't
    include job descriptions, so for postings whose title already looks
    relevant (_title_looks_relevant), a follow-up request to the job's own
    detail page fetches the full description — both to catch clearance
    requirements that aren't mentioned in the title, and to look for a
    salary range (see _extract_salary) — same pattern as _fetch_workday_jobs,
    and for the same avoid-an-extra-request-per-irrelevant-posting reason.
    Always fetched now; previously skipped when _clearance_screening_needed()
    was False, back when clearance was the description's only consumer.
    Postings are still dropped by _builtin_location_matches (BUILTIN_LOCATION /
    BUILTIN_WORK_TYPE env vars) before the description fetch, for cost
    avoidance.

    Args:
        careers_url: A Built In search URL, e.g.
            https://builtin.com/jobs?search=AWS&daysSinceUpdated=3

    Returns:
        Normalised list of job dicts with title, url, location, and company
        keys (plus clearance_review=True for an ambiguous clearance mention,
        and salary when a pay range is found in the description).
    """
    known_companies = _get_known_company_names()

    jobs = []
    location_skipped = 0
    clearance_skipped = 0
    for page in range(1, _BUILTIN_MAX_PAGES + 1):
        try:
            resp = requests.get(
                careers_url,
                params={"page": page},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Built In fetch failed", url=careers_url, page=page, error=str(exc))
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select('[data-id="job-card"]')
        if not cards:
            break

        for card in cards:
            title_el = card.select_one('[data-id="job-card-title"]')
            company_el = card.select_one('[data-id="company-title"]')
            if not title_el or not company_el:
                continue
            company = company_el.get_text(strip=True)
            if _is_known_company(company, known_companies):
                continue
            title = title_el.get_text(strip=True)
            if not _title_looks_relevant(title):
                continue
            geo = _builtin_card_text_by_icon(card, "fa-location-dot")
            workplace = _builtin_card_text_by_icon(card, "fa-house-building")
            # Built In shows these as two separate badges — geography (e.g.
            # "USA") and work model (e.g. "Remote") — verified directly:
            # every card checked had both, and the geography badge alone
            # rarely contains "remote" even for fully-remote roles, which
            # silently excluded about half of genuinely-remote postings
            # under the work-type filter before this was combined.
            location = f"{geo} ({workplace})" if geo and workplace else geo or workplace
            if not _builtin_location_matches(location):
                location_skipped += 1
                continue
            href = title_el.get("href", "")
            job_url = _BUILTIN_BASE_URL + (href if isinstance(href, str) else "")
            description = _fetch_builtin_job_description(job_url)
            excluded, needs_review = _clearance_decision(f"{title} {description}")
            if excluded:
                clearance_skipped += 1
                continue
            job = {
                "title": title,
                "url": job_url,
                "location": location,
                "company": company,
            }
            if needs_review:
                job["clearance_review"] = True
            salary = _extract_salary(description)
            if salary:
                job["salary"] = salary
            jobs.append(job)

    logger.info(
        "Built In jobs fetched",
        url=careers_url,
        count=len(jobs),
        location_skipped=location_skipped,
        clearance_skipped=clearance_skipped,
    )
    return jobs


def _fetch_oracle_jobs(careers_url: str) -> list[dict[str, Any]]:
    """Fetch job listings from an Oracle Fusion Cloud Recruiting careers site.

    Parses the host/locale/site from a hcmUI/CandidateExperience careers URL,
    then issues one paginated search per TITLE_KEYWORDS entry against
    Oracle's public recruitingCEJobRequisitions REST API — the same endpoint
    the career site's own search page calls, no auth or bot protection.
    Like Workday, the keyword search is fuzzy, not an exact substring match,
    so every result is still re-checked with _title_looks_relevant before
    being kept, and seen_ids dedupes postings surfaced under more than one
    keyword. Unlike Workday/Built In, the search response already includes
    each posting's full description (ShortDescriptionStr), so no extra
    per-posting detail request is needed — same as Greenhouse.

    Args:
        careers_url: Careers URL of the form
            https://{host}/hcmUI/CandidateExperience/{locale}/sites/{site}.

    Returns:
        Normalised list of job dicts with title, url, location keys (plus
        clearance_review=True for jobs with an ambiguous clearance mention,
        and salary when a pay range is found in the description — see
        _extract_salary).
    """
    match = _ORACLE_URL_RE.match(careers_url)
    if not match:
        logger.warning("Not a parseable Oracle CandidateExperience URL", url=careers_url)
        return []
    host, locale, site = match.groups()
    api_url = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    jobs = []
    clearance_skipped = 0
    seen_ids: set[str] = set()

    for keyword in _title_keywords():
        offset = 0
        while offset < _ORACLE_MAX_JOBS_PER_KEYWORD:
            try:
                resp = requests.get(
                    api_url,
                    params={
                        "onlyData": "true",
                        "expand": "requisitionList",
                        "finder": (
                            f"findReqs;siteNumber={site},facetsList=LOCATIONS,"
                            f"limit={_ORACLE_PAGE_SIZE},offset={offset},keyword={keyword}"
                        ),
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                logger.warning("Oracle fetch failed", url=api_url, keyword=keyword, error=str(exc))
                break

            try:
                data = resp.json()
                item = data["items"][0]
            except (requests.exceptions.JSONDecodeError, KeyError, IndexError):
                logger.warning("Oracle response is not in the expected shape", url=api_url, keyword=keyword)
                break

            postings = item.get("requisitionList", [])
            if not postings:
                break

            for posting in postings:
                job_id = posting.get("Id", "")
                if job_id in seen_ids:
                    continue
                title = posting.get("Title", "")
                if not _title_looks_relevant(title):
                    continue
                seen_ids.add(job_id)
                description = posting.get("ShortDescriptionStr") or ""
                excluded, needs_review = _clearance_decision(f"{title} {description}")
                if excluded:
                    clearance_skipped += 1
                    continue
                job = {
                    "title": title,
                    "url": f"https://{host}/hcmUI/CandidateExperience/{locale}/sites/{site}/job/{job_id}",
                    "location": posting.get("PrimaryLocation", ""),
                }
                if needs_review:
                    job["clearance_review"] = True
                salary = _extract_salary(description)
                if salary:
                    job["salary"] = salary
                jobs.append(job)

            offset += _ORACLE_PAGE_SIZE
            if offset >= item.get("TotalJobsCount", 0):
                break

    logger.info("Oracle jobs fetched", url=careers_url, count=len(jobs), clearance_skipped=clearance_skipped)
    return jobs


def _fetch_jobs(company_name: str, careers_url: str, ats: str) -> list[dict[str, Any]]:
    """Dispatch to the appropriate ATS handler and return normalised job dicts.

    Args:
        company_name: Unused; kept for a uniform call signature across backends.
        careers_url: URL passed to the ATS handler.
        ats: ATS backend identifier ("greenhouse", "lever", "workday", "builtin", or "oracle").

    Returns:
        Normalised list of job dicts with title, url, location keys (plus a
        "company" key for the "builtin" backend, which aggregates postings
        across many employers). Unrecognised ats values yield no jobs.
    """
    if ats == "greenhouse":
        return _fetch_greenhouse_jobs(careers_url)
    if ats == "lever":
        return _fetch_lever_jobs(careers_url)
    if ats == "workday":
        return _fetch_workday_jobs(careers_url)
    if ats == "builtin":
        return _fetch_builtin_jobs(careers_url)
    if ats == "oracle":
        return _fetch_oracle_jobs(careers_url)
    logger.warning("Unrecognised ATS backend", company=company_name, ats=ats)
    return []


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for the Worker Lambda.

    Processes each SQS record, fetches jobs via the appropriate ATS handler,
    applies the relevance filter, and persists new job postings to DynamoDB.

    Args:
        event: SQS event containing one or more Records.
        context: Lambda context object (unused).

    Returns:
        A summary dict with counts of records processed and jobs written.
    """
    jobs_table_name = os.environ["JOBS_TABLE"]
    table = dynamodb.Table(jobs_table_name)

    records_processed = 0
    jobs_written = 0

    for record in event.get("Records", []):
        body = json.loads(record["body"])
        company_name: str = body["company_name"]
        careers_url: str = body["careers_url"]
        ats: str = body.get("ats", "unknown")

        logger.info("Processing company", company=company_name, url=careers_url, ats=ats)

        jobs = _filter_relevant_jobs(
            _fetch_jobs(company_name, careers_url, ats),
            company_name,
        )

        for job in jobs:
            # "builtin" jobs carry their own company (Built In aggregates across
            # employers); every other backend's jobs belong to company_name.
            job_company = job.get("company") or company_name
            job_id = _make_job_id(job_company, job["title"], job["url"])
            item = {
                "job_id": job_id,
                "company": job_company,
                "title": job["title"],
                "url": job["url"],
                "location": job.get("location", ""),
                "discovered_at": datetime.now(UTC).isoformat(),
            }
            if job.get("clearance_review"):
                item["clearance_review"] = True
            if job.get("salary"):
                item["salary"] = job["salary"]
            # condition_expression prevents overwriting existing items
            try:
                table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(job_id)",
                )
                jobs_written += 1
                logger.info("Wrote new job", title=job["title"], company=job_company)
            except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                logger.debug("Duplicate skipped", job_id=job_id)

        records_processed += 1

    logger.info("Worker done", records_processed=records_processed, jobs_written=jobs_written)
    return {"records_processed": records_processed, "jobs_written": jobs_written}
