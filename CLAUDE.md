# CLAUDE.md

## Project overview

terraform-aws-req-aggregator is an AWS serverless application that monitors company careers pages and emails a daily digest of new job postings. It uses three Lambda functions orchestrated by EventBridge, with SQS for fan-out, DynamoDB for storage, and SES for email delivery.

```
EventBridge cron → Orchestrator Lambda → SQS (1 msg/company)
                                              ↓
                    Worker Lambda (Greenhouse/Lever/Workday/Built In APIs)
                                              ↓
                                       DynamoDB jobs table
                                              ↑
EventBridge cron (+30min) → Notifier Lambda → SES email digest
```

## Repo layout

```
main.tf                          # all resources (IAM, Lambda, SQS, DynamoDB, EventBridge) + self-build wiring
variables.tf
outputs.tf
versions.tf                      # required_providers only — no backend, no provider block
scripts/
  build-lambda-package.sh        # builds each Lambda's zip automatically during plan/apply
examples/
  complete/
    main.tf                      # every variable, fully commented — own provider block, local state, not for real deploys
    README.md
companies/
  companies.json                 # example seed data documenting the expected schema (not consumed by this module directly)
src/
  conftest.py                    # sets fake AWS creds at module level for pytest
  orchestrator/
    orchestrator/handler.py      # scans companies table, fans out SQS
    tests/test_handler.py
  worker/
    worker/handler.py            # _fetch_jobs dispatches to an ATS fetcher, writes jobs
    tests/test_handler.py
    tests/conftest.py            # sets required LOCATION/WORK_TYPE/TITLE_KEYWORDS/etc. env vars for tests
  notifier/
    notifier/handler.py          # queries recent jobs, sends SES digest
    tests/test_handler.py
```

## Development commands

```bash
uv sync --all-packages           # install all workspace packages + dev deps
uv run pytest                    # run tests
uv run pytest --tb=short -q      # terse output
uv run ruff check src/           # lint
uv run ruff format src/          # format
uv run ty check src/             # type check
uv run pre-commit run --all-files  # run all pre-commit hooks manually
terraform fmt -check && terraform validate    # from repo root — no -chdir needed anymore
cd examples/complete && terraform init && terraform plan   # exercises the module end-to-end (builds real Lambda zips, fails at the AWS-auth step without real credentials — that's expected)
```

## Python conventions

- **uv workspace** with three packages under `src/`. Each Lambda has its own `pyproject.toml` so dependencies are isolated per function.
- All handlers follow the same pattern: module-level boto3 clients (`dynamodb = boto3.resource("dynamodb")`), env vars read inside the handler function.
- Job deduplication key: `SHA-256(company|title|url)` → `job_id` DynamoDB partition key. See `worker/handler.py:_make_job_id`.
- `worker/handler.py:_fetch_jobs` dispatches on the `ats` field to one of: `greenhouse`, `lever`, `workday` (all JSON API calls), `builtin` (scrapes a Built In search results page — server-rendered HTML; aggregates across employers so each job carries its own `company` key and jobs from companies already tracked directly are skipped, via a `dynamodb:Scan` on `COMPANIES_TABLE`), or `oracle` (Oracle Fusion Cloud Recruiting's public `recruitingCEJobRequisitions` REST API — the same unauthenticated endpoint the career site's own search page calls). Any other `ats` value (including the historical `unknown` default) logs a warning and returns no jobs — there used to be a Playwright + Strands/Bedrock (Claude Haiku) fallback here, but it was removed: it fundamentally could never extract a job, since it built the LLM's page text via BeautifulSoup's `get_text()`, which strips every `href` attribute, while the prompt required URLs to appear verbatim in that same text.
- `worker/handler.py:_fetch_workday_jobs` and `_fetch_oracle_jobs` both issue one paginated search per `TITLE_KEYWORDS` entry (Workday's `searchText` param / Oracle's `finder=findReqs;...,keyword=` param) instead of paginating a company's entire board unfiltered — company board sizes vary from a few hundred to 17,000+ (e.g. CVS, a posting per retail store), and each platform's own search narrows results server-side to a manageable subset regardless of company size, where blanket pagination would either miss postings past an arbitrary cutoff or blow the Lambda timeout entirely. Both searches are fuzzy full-text, not exact-substring, so every result is still re-checked with `_title_looks_relevant` before being kept; `seen_paths`/`seen_ids` dedupe postings that surface under more than one keyword so they're not reprocessed. Unlike Workday (and Built In), Oracle's search response already includes each posting's full description (`ShortDescriptionStr`), so `_fetch_oracle_jobs` needs no extra per-posting detail request at all — same efficiency as Greenhouse's `content=true`.
- `TITLE_KEYWORDS`/`EXCLUDE_TITLE_KEYWORDS` env vars (via `_title_keywords()`/`_exclude_title_keywords()`, same read-fresh-every-call pattern as `LOCATION`/`WORK_TYPE`) define what job category the app hunts for — default to the tech/infra keyword set. This is the one piece of config that has to change to repurpose the app for a different search (e.g. nursing), everything else (ATS backends, location/work-type filtering, clearance filtering) is generic.
- Clearance filtering is tiered, not binary: `_clearance_tier(text)` classifies text as `"top_secret"`, `"secret"`, `"public_trust"`, `"ambiguous"` (a generic/unspecified mention with no level given, e.g. "security clearance required"), or `"none"`, checked highest tier first so overlapping substrings (e.g. "top secret clearance" also containing "secret clearance") resolve to the higher tier. `_clearance_decision(text) -> (excluded, needs_review)` then applies the matching `ALLOW_PUBLIC_TRUST`/`ALLOW_SECRET_CLEARANCE`/`ALLOW_TOP_SECRET_CLEARANCE` env var (wired from `var.allow_public_trust`/`var.allow_secret_clearance`/`var.allow_top_secret_clearance`) for a known tier; an `"ambiguous"` mention is never excluded outright (guessing risks hiding a posting the user would've been fine with) — instead `needs_review=True` unless every tier is already allowed (`_clearance_screening_needed()` is `False`), in which case there's nothing left to resolve. Each ATS fetcher owns its own clearance decision, not `_filter_relevant_jobs`: `_fetch_greenhouse_jobs` and `_fetch_oracle_jobs` check the full job description at no extra request cost (Greenhouse's list API returns it via `content=true`; Oracle's search response already includes `ShortDescriptionStr`); `_fetch_workday_jobs` and `_fetch_builtin_jobs` also check it, but each via a per-posting follow-up request to the job's own detail page/endpoint — only for postings whose title already passes `_title_looks_relevant`, and skipped entirely when `_clearance_screening_needed()` is `False`; `_fetch_lever_jobs` is title-only (its list response has no description). A `needs_review` job gets `clearance_review=True` on its dict, persisted to DynamoDB by `handler()` and rendered as a "CLEARANCE UNCLEAR" badge in the notifier digest (`notifier/handler.py:_build_email_body`) instead of being silently guessed at.
- `worker/handler.py:_filter_relevant_jobs` also drops jobs whose `location` matches `_is_non_us_location` — a word-boundary regex over a curated list of countries/regions/offshore-hub cities (`_NON_US_LOCATION_KEYWORDS`). Defaults to keeping ambiguous locations (bare "Remote", "N Locations", empty) rather than risk hiding a real US posting; deliberately omits US/non-US-ambiguous names (e.g. "Georgia") for the same reason.
- `_fetch_builtin_jobs` additionally applies `_builtin_location_matches`, controlled by the `BUILTIN_LOCATION` (default `""` — disabled) and `BUILTIN_WORK_TYPE` (default `"remote"`) env vars. Built In's own location/remote-preference search filters are client-side JS and are silently ignored by a plain `requests.get` (verified directly), so this is enforced locally against the location text already scraped per job card. Defaults to remote-only, matching the user's manual builtin.com search practice (blank location, Remote checked). Built In renders geography (`fa-location-dot`, e.g. "USA") and work model (`fa-house-building`, e.g. "Remote") as two separate badges on each card; the location text passed to `_builtin_location_matches` combines both (`"USA (Remote)"`) since the geography badge alone rarely contains the literal word "remote" even for fully-remote postings.
- `worker/handler.py:_filter_relevant_jobs` also applies `_location_matches` (`LOCATION`/`WORK_TYPE` env vars, same default remote-only behavior) to every backend except `builtin`, which is exempt — detected via the per-job `"company"` key that only `_fetch_builtin_jobs` sets — since it's already filtered by its own independent `BUILTIN_LOCATION`/`BUILTIN_WORK_TYPE` config. The two settings are deliberately kept separate: the curated company list includes companies chosen for proximity to a future physical location, so a hybrid/on-site preference there shouldn't share Built In's "remote only" default. Both `_location_matches` and `_builtin_location_matches` delegate to the shared `_work_type_matches(location, location_env_var, work_type_env_var)`.
- `worker/handler.py` has no config defaults of its own — `LOCATION`/`WORK_TYPE`/`BUILTIN_LOCATION`/`BUILTIN_WORK_TYPE`/`TITLE_KEYWORDS`/`EXCLUDE_TITLE_KEYWORDS`/`ALLOW_PUBLIC_TRUST`/`ALLOW_SECRET_CLEARANCE`/`ALLOW_TOP_SECRET_CLEARANCE` are all required env vars (plain `os.environ[...]`, not `.get(..., default)`), 12-factor-style — the actual default *values* live only in `variables.tf`, supplied by Terraform. `src/worker/tests/conftest.py` sets baseline values for these at module level so the test suite exercises realistic behavior without every test setting them individually; tests needing a different value still use `monkeypatch.setenv`.
- `notifier/handler.py:_build_email_body` renders an HTML digest (styled, table-based for email-client compatibility) plus a plain-text fallback, both grouped by company with location shown. All interpolated values are HTML-escaped.
- Dedup between the Worker and Notifier is state-based, not time-based: `worker/handler.py:handler` writes every new job with `sent_in_digest=False` and `digest_pending="pending"`; `notifier/handler.py:_query_pending_jobs` Queries the `pending-digest-index` GSI (hash key `digest_pending`, range key `discovered_at`) instead of scanning the table. The GSI is deliberately sparse — `digest_pending` only exists on unsent jobs — so the index never grows to include already-sent jobs regardless of table size, and `_mark_jobs_sent` (called after a successful SES send) `REMOVE`s `digest_pending` (rather than just setting it falsy) to drop each emailed job out of the index and flips `sent_in_digest` to `True`. This replaced an earlier `LOOKBACK_MINUTES`-based time-window Scan, which silently dropped jobs whose `discovered_at` write landed outside the window (e.g. after a Worker SQS retry) since nothing ever revisited them.
- Lambda handlers return a summary dict (`{"published": n}` etc.) for easy CloudWatch Insights querying.

## Testing conventions

- Tests use **moto** (`@mock_aws` / `with mock_aws()`) for all AWS service mocking — no MagicMock for boto3 calls.
- `src/conftest.py` sets fake AWS credentials and `AWS_CONFIG_FILE=/dev/null` at **module level** (not in a fixture). This is required because handlers create boto3 clients at import time; a fixture would run too late.
- `src/worker/tests/conftest.py` (worker-package-scoped, not the root one) sets required `LOCATION`/`WORK_TYPE`/`BUILTIN_LOCATION`/`BUILTIN_WORK_TYPE`/`TITLE_KEYWORDS`/`EXCLUDE_TITLE_KEYWORDS`/`ALLOW_PUBLIC_TRUST`/`ALLOW_SECRET_CLEARANCE`/`ALLOW_TOP_SECRET_CLEARANCE` env vars at module level, since `worker/handler.py` has no defaults of its own for these.
- Each test file has an `aws_resources` pytest fixture that creates real moto-backed infrastructure inside `with mock_aws(): ... yield`. Tests run inside that context.
- The `greenhouse`/`lever`/`workday`/`builtin` fetchers make plain `requests` calls and are tested by mocking `requests` directly; `builtin` additionally touches DynamoDB (scans `COMPANIES_TABLE`), which is backed by moto like everything else.
- pytest uses `--import-mode=importlib` (set in root `pyproject.toml`) to avoid module name collisions across the three `test_handler.py` files.
- Add tests for every new handler behaviour; verify state in DynamoDB/SQS directly rather than asserting on mock call counts.

## Terraform conventions

- This repo is a **standalone callable module** (repo root = module root, matching the `terraform-aws-modules` convention) — `versions.tf` declares only `required_providers`, deliberately no `provider` block and no `backend` block. A `provider`/`backend` block inside a module either breaks the consumer's ability to use `count`/`for_each`/`depends_on` on the module call, or (for backend) is a hard Terraform error when the module is referenced via `source = ...`. Both belong exclusively to whatever root configuration instantiates this module.
- All resources use `local.prefix` (`= var.prefix`, default `"req-aggregator"`) for naming — deliberately independent of the repo/module name.
- **Lambda packaging is fully automatic, no separate build step.** `data.external.lambda_build` (in `main.tf`, `for_each` over the three function names) invokes `scripts/build-lambda-package.sh <name>` during every `plan`/`apply`; the script hashes `src/{name}/`, only re-runs `pip3 install --platform manylinux2014_x86_64 --python-version 3.13 --implementation cp --only-binary=:all: --target .build/{name} src/{name}` + `zip` when that hash changed, and always reports the built zip's base64 SHA-256 (matching `filebase64sha256()`'s format) as the `external` data source's result. Each Lambda's `source_code_hash` reads `data.external.lambda_build["{name}"].result.hash`; `filename` stays a plain `${path.module}/.build/{name}.zip` reference. This deliberately avoids a `null_resource` + `depends_on` + `filebase64sha256()` design, which hits a chicken-and-egg failure on a fresh checkout (`filebase64sha256()` evaluates during plan regardless of `depends_on`, before the zip exists) — a `data` source has no such ordering problem, since the script producing the zip IS the thing reporting its hash. Because referencing this module via a bare git source (no `//subpath`, repo root = module root) clones the whole repo including `src/` into the consumer's `.terraform/modules/` cache, `${path.module}/src/{name}` reaches the right code with zero separate distribution mechanism — confirmed working end-to-end via `examples/complete/`. Requires `bash`, `pip3`/`python3.13`, `zip`, `openssl` on whatever machine runs `terraform apply`. The worker previously shipped as a container image (needed for a since-removed Playwright + Bedrock dependency — see the `worker/handler.py` note above); that was dropped once the dependency was gone, and this `data.external` design later replaced the plain-zip-plus-manual-build-step approach that followed it, removing the last manual pre-`apply` step entirely.
- IAM policies follow least-privilege per Lambda.
- SQS queues use `sqs_managed_sse_enabled = true`.
- DynamoDB tables use `PAY_PER_REQUEST` billing.

## Open TODOs

- `orchestrator/handler.py`: add DynamoDB scan pagination for large company lists.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs two jobs on PRs and pushes to main:
1. **pre-commit** — runs all hooks (`ruff`, `ty`, `terraform_fmt`, `terraform_validate`, `terraform_docs`, `terraform_tflint`, `terraform_checkov`, `check-merge-conflict`, `end-of-file-fixer`). Requires terraform, tflint (v0.55.0), checkov (v3.2.526), and terraform-docs (v0.24.0) installed as separate steps before the pre-commit run. The `terraform_docs` hook shells out to whatever `terraform-docs` is on PATH (no bundled/pinned version of its own), so this CI pin must be kept in sync with whatever version is used for local development — a mismatch causes CI to "detect" a diff in `README.md` that doesn't reproduce locally. No Lambda-ZIP pre-build step is needed here anymore: `terraform_validate` never evaluates `data` sources, so `data.external.lambda_build` is never invoked during `validate`.
2. **Tests** — `uv run pytest --tb=short -q`

pytest (`stages: [pre-push]`) is excluded from the pre-commit job since it runs in the dedicated test job.
