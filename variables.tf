variable "aws_region" {
  description = "AWS region to deploy resources into"
  type        = string
  default     = "us-east-1"
}

variable "prefix" {
  description = "Prefix used to name every AWS resource (Lambda functions, DynamoDB tables, SQS queues, etc.), independent of the repo/module name"
  type        = string
  default     = "req-aggregator"
}

variable "enable_dashboard" {
  description = "Whether to create the CloudWatch observability dashboard. It's built entirely from standard AWS-published metrics and Logs Insights queries (no custom metrics), so it costs nothing beyond the free tier when unused — this exists to avoid spending one of the 3 free dashboards/account on it for module users who don't want it"
  type        = bool
  default     = true
}

variable "enable_cost_widget" {
  description = "Whether to add a Cost Explorer widget (via the ccliver/cw-cost-widget/aws module) to the observability dashboard. Defaults to false (unlike enable_dashboard) because it requires a one-time manual step outside Terraform — activating cost_allocation_tag_key as a Cost Allocation Tag in AWS Billing — and shows no data until that's done and Cost Explorer has accrued cost from activation forward. Has no effect when enable_dashboard is false."
  type        = bool
  default     = false
}

variable "cost_allocation_tag_key" {
  description = "Cost allocation tag key the cost widget filters AWS Cost Explorer by. Must match a tag key actually applied to this module's billed resources (e.g. via default_tags in the calling provider block) and activated as a Cost Allocation Tag in AWS Billing. Only used when enable_cost_widget is true."
  type        = string
  default     = "Project"
}

variable "cost_allocation_tag_values" {
  description = "Cost allocation tag values to filter Cost Explorer by. Defaults to [var.prefix] when null, matching the common default_tags pattern of tagging every resource with the module's prefix (e.g. Project = local.prefix). Only used when enable_cost_widget is true."
  type        = list(string)
  default     = null
}

variable "ses_from_address" {
  description = "Verified SES sender email address"
  type        = string
}

variable "ses_to_address" {
  description = "Recipient email address for job digests"
  type        = string
}

variable "orchestrator_weekday_schedule" {
  description = "EventBridge cron expression for the Orchestrator Lambda on weekdays"
  type        = string
  default     = "cron(0 9 ? * MON-FRI *)" # once at 9am, Mon-Fri
}

variable "orchestrator_weekend_schedule" {
  description = "EventBridge cron expression for the Orchestrator Lambda on weekends. Set to null to disable weekend runs entirely."
  type        = string
  default     = null
}

variable "notifier_weekday_schedule" {
  description = "EventBridge cron expression for the Notifier Lambda on weekdays (30 min after orchestrator)"
  type        = string
  default     = "cron(30 9 ? * MON-FRI *)"
}

variable "notifier_weekend_schedule" {
  description = "EventBridge cron expression for the Notifier Lambda on weekends (30 min after orchestrator). Set to null to disable weekend runs entirely."
  type        = string
  default     = "cron(30 8 ? * SAT-SUN *)"
}

variable "schedule_timezone" {
  description = "IANA timezone the schedule cron expressions are evaluated in (EventBridge Scheduler handles DST automatically)"
  type        = string
  default     = "America/New_York"
}

variable "lookback_minutes" {
  description = "Minutes the Notifier looks back when querying for new jobs"
  type        = number
  default     = 60
}

variable "builtin_location" {
  description = "Location substring to additionally keep for the Built In (builtin.com) ATS backend; blank disables it (remote-only)"
  type        = string
  default     = ""
}

variable "builtin_work_type" {
  description = "Work-type keyword to keep for the Built In ATS backend (remote, hybrid, office, any, or any literal substring)"
  type        = string
  default     = "remote"
}

variable "location" {
  description = "Comma-separated location substrings (OR'd together) to additionally keep for every ATS backend except builtin; blank disables it (remote-only). Independent of builtin_location"
  type        = string
  default     = ""
}

variable "work_type" {
  description = "Work-type keyword to keep for every ATS backend except builtin (remote, hybrid, office, any, or any literal substring). Independent of builtin_work_type"
  type        = string
  default     = "remote"
}

variable "title_keywords" {
  description = "Comma-separated title substrings (OR'd together, case-insensitive) a job title must match at least one of to be kept at all; also drives one paginated Workday/Oracle search per entry"
  type        = string
  default     = "platform,sre,site reliability,devops,cloud engineer,infrastructure,staff engineer"
}

variable "exclude_title_keywords" {
  description = "Comma-separated title substrings (OR'd together, case-insensitive); a title matching any of these is dropped even if it also matched title_keywords"
  type        = string
  default     = "manager,director"
}

# A posting with a generic/unspecified clearance mention (no level stated) is
# never dropped by any of these three — the tier can't be determined from
# text alone, so it's kept and flagged for manual review in the notifier
# digest instead, unless every tier below is already allowed.
variable "allow_public_trust" {
  description = "Whether to keep job postings that require a Public Trust clearance"
  type        = bool
  default     = true
}

variable "allow_secret_clearance" {
  description = "Whether to keep job postings that require a Secret-tier clearance (Secret, DoD Secret, Interim Secret, or the DOE-equivalent L clearance) — no polygraph or friends/family interviews required"
  type        = bool
  default     = false
}

variable "allow_top_secret_clearance" {
  description = "Whether to keep job postings that require a Top-Secret-tier or above clearance (Top Secret, TS/SCI, a polygraph, a Special Access Program, or the DOE-equivalent Q clearance)"
  type        = bool
  default     = false
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout in seconds"
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB (orchestrator and notifier)"
  type        = number
  default     = 512
}

variable "worker_memory_mb" {
  description = "Worker Lambda memory in MB"
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention in days for the orchestrator, worker, and notifier Lambda log groups"
  type        = number
  default     = 30
}
