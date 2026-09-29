# Streamlit Job Tracker

A local Streamlit UI for the [application-tracking API](../../README.md#application-tracking) (`enable_tracking_api`): browse/filter jobs and edit `status`/`date_applied`/`response_date`/`notes` against a posting, without hand-crafting SigV4-signed `curl` requests.

Every request runs locally and is signed with your own AWS credentials (SigV4, service `execute-api`) via boto3's default credential chain — there's no login/auth UI of its own, no API key, and nothing but the signed HTTP request leaves your machine.

## Prerequisites

- The module deployed with `enable_tracking_api = true` (see [`examples/complete/main.tf`](../complete/main.tf)) and its `tracking_api_invoke_url` output.
- AWS credentials available to boto3's default chain (environment variables, `~/.aws/credentials`, SSO, etc.) for an identity with `execute-api:Invoke` on that API's ARN, and a configured region (`AWS_REGION`/`AWS_DEFAULT_REGION`, or `region` in your profile) matching where the module was deployed. Example IAM statement, scoped to just this API:
  ```json
  {
    "Effect": "Allow",
    "Action": "execute-api:Invoke",
    "Resource": "arn:aws:execute-api:<region>:<account-id>:<api-id>/*/*/jobs*"
  }
  ```

## Configuration

The API's invoke URL is read from (in order of precedence): the `API_URL` env var, `.streamlit/secrets.toml`'s `API_URL` key, then whatever's typed into the sidebar field — never hardcoded. To use `secrets.toml`:

```bash
mkdir -p .streamlit
echo 'API_URL = "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com"' > .streamlit/secrets.toml
```

## Run

**Option 1: `task run`** (reads the invoke URL from Terraform automatically). Requires [go-task](https://taskfile.dev) (`brew install go-task`):

```bash
cd examples/streamlit
task run                                   # reads ../complete's Terraform output
task run TF_DIR=/path/to/your/config-repo  # or point at wherever you actually applied
                                            # the module with enable_tracking_api = true —
                                            # e.g. a separate private config repo, if that's
                                            # where your real (non-example) deployment lives
```

This runs `terraform -chdir=TF_DIR output -raw tracking_api_invoke_url`, sets it as the `API_URL` env var, and launches the app — installing `requirements.txt` into a local `.venv` the first time it's run. It only reads existing Terraform state; it doesn't run `apply` for you.

**Option 2: plain pip** (paste the URL in by hand, or use `.streamlit/secrets.toml` above):

```bash
cd examples/streamlit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Either way, Streamlit opens a browser tab at `http://localhost:8501`.

## Using it

- **Sidebar** — the invoke URL (pre-filled if configured per above), who you're signed in as (via `sts:GetCallerIdentity`, which needs no IAM grant of its own), and filters: status, a company/title search, and a `discovered_at` date range. Status and search filter instantly (no request); changing the date range needs **Search**.
- **Summary tiles** — Total / Not Applied / Applied / Interviewing / Offer / Rejected, each showing counts across *every* fetched job regardless of the active filter (so switching between them never shows a stale/zeroed count). "Not Applied" includes jobs the Worker wrote that nobody's touched yet, not just ones explicitly set to it — the API treats those the same way (see the root README's Application Tracking section). Click a tile's **View →** to jump the status filter straight to it — this also updates the page URL (`?status=applied`), so a filtered view is bookmarkable/shareable.
- **Main table** — rows are tinted by status (applied/interviewing/rejected/offer each get their own color; not_applied is left untinted, since it's the default/no-action-yet state). `company`/`title`/`location`/`discovered_at` (date only)/a link to the posting/`salary` (auto-detected from the posting by the Worker — there's no separate user-entered salary field; use `notes` for anything you learn during the process, e.g. from a recruiter call) are read-only. `status` is editable directly in the cell as a dropdown constrained to `not_applied`/`applied`/`interviewing`/`rejected`/`offer`, matching what the API accepts — including reverting a job back to `not_applied` if you change your mind. The leftmost ✏️ column pops a row open in a focused dialog to edit `date_applied`/`response_date`/`notes` — those aren't inline-editable in the table itself, since Streamlit only tints non-editable columns, and coloring the whole row mattered more than inline-editing every field. Click a column header to sort by it.
- Click **Save changes** after editing `status` inline for as many rows as you like — only cells that actually changed are sent, one `PATCH` per edited row. Nothing saves until you click it.
- **Export CSV** — downloads whatever's currently visible (after status/search filtering) as a CSV.
- Clearing a text cell (in the dialog) back to empty and saving sends an empty string — the closest this API gets to "unsetting" a field, since it only supports `SET`. `status` can't be blank at all (there's no "" option) — pick `not_applied` instead of trying to clear it.
- API errors (4xx/5xx) show as a plain error message, not a stack trace.

### Other ideas not built here

A few more things that would be reasonable to add if useful: clearance-tier badges from the worker's `clearance_tier`/`clearance_review` fields, bulk status updates across multiple selected rows, and a "saved view" (persisting a filter combination as a named URL).

## Styling

`.streamlit/config.toml` sets a Monokai-inspired dark theme. Edit its `[theme]` colors directly, or delete the file to fall back to Streamlit's default theme.
