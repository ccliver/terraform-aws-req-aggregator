locals {
  prefix = "req-aggregator-example"
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = local.prefix
      ManagedBy = "terraform"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

module "req_aggregator" {
  source = "../.."

  # --- Core ---
  aws_region       = var.aws_region
  prefix           = local.prefix # names every AWS resource; independent of the repo/module name
  enable_dashboard = true         # built entirely from free AWS-published metrics/log queries; set false to skip using one of the 3 free dashboards/account

  # --- Cost widget (off by default — needs the Project tag activated as a Cost Allocation Tag in AWS Billing first) ---
  enable_cost_widget         = false
  cost_allocation_tag_key    = "Project"      # matches default_tags below
  cost_allocation_tag_values = [local.prefix] # or leave unset (null) to default to [prefix] automatically

  # --- SES (required, no default — verify both addresses in SES first) ---
  ses_from_address = "you@yourdomain.com"
  ses_to_address   = "you@yourdomain.com"

  # --- Schedules (EventBridge Scheduler cron expressions + timezone) ---
  orchestrator_weekday_schedule = "cron(0 8-18/2 ? * MON-FRI *)"  # every 2 hrs, 8am-6pm, weekdays
  orchestrator_weekend_schedule = "cron(0 8 ? * SAT-SUN *)"       # once at 8am, weekends
  notifier_weekday_schedule     = "cron(30 8-18/2 ? * MON-FRI *)" # 30 min after orchestrator
  notifier_weekend_schedule     = "cron(30 8 ? * SAT-SUN *)"
  schedule_timezone             = "America/New_York" # EventBridge Scheduler handles DST automatically
  lookback_minutes              = 60                 # how far back the Notifier looks for new jobs

  # --- Location / work-type filtering ---
  # location/work_type apply to every ATS backend except builtin; builtin_location/
  # builtin_work_type are independent, since Built In is a broad discovery search
  # where "remote only" is a sensible default even when the curated company list
  # (location/work_type) targets a specific place instead.
  location          = ""       # comma-separated substrings, e.g. "Reston, VA,Arlington, VA"; blank disables it (remote-only)
  work_type         = "remote" # "remote" | "hybrid" | "office" | "any" | any literal substring
  builtin_location  = ""       # same shape as location, independent setting, for the builtin backend only
  builtin_work_type = "remote"

  # --- What job category this instance hunts for ---
  # This is the one setting you'd change to repurpose the app entirely (e.g.
  # for nursing instead of tech roles) — everything else here is generic.
  title_keywords         = "platform,sre,site reliability,devops,cloud engineer,infrastructure,staff engineer"
  exclude_title_keywords = "manager,director" # dropped even if they also match title_keywords

  # --- Clearance filtering ---
  # An ambiguous/unspecified clearance mention is never dropped by these — it's
  # kept and flagged for manual review in the notifier digest instead.
  allow_public_trust         = true  # no polygraph or friends/family interviews
  allow_secret_clearance     = false # also no polygraph/interviews required, but not yet worth pursuing
  allow_top_secret_clearance = false # requires a polygraph and friends/family interviews

  # --- Lambda sizing ---
  lambda_timeout_seconds = 300
  lambda_memory_mb       = 512 # orchestrator + notifier
  worker_memory_mb       = 512

  # --- Observability ---
  log_retention_days = 30
}

output "dashboard_url" {
  value = module.req_aggregator.dashboard_url
}

output "companies_table_name" {
  value = module.req_aggregator.companies_table_name
}

output "jobs_table_name" {
  value = module.req_aggregator.jobs_table_name
}
