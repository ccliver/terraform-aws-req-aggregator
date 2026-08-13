locals {
  prefix       = var.prefix
  lambda_names = ["orchestrator", "worker", "notifier"]
}

data "aws_caller_identity" "current" {}

# Builds each Lambda's dependency-bundled zip automatically as part of
# `terraform plan`/`apply` (see scripts/build-lambda-package.sh) — no separate
# build step required, so `terraform init && terraform apply` alone is enough
# for a consumer of this module. A null_resource + depends_on + filebase64sha256()
# would hit a chicken-and-egg failure on a fresh checkout (filebase64sha256 is
# evaluated during plan regardless of depends_on, before the zip exists); a
# data source has no such ordering problem since the script that builds the zip
# is the same thing that reports its hash, re-run fresh on every plan/apply.
data "external" "lambda_build" {
  for_each = toset(local.lambda_names)
  program  = ["${path.module}/scripts/build-lambda-package.sh", each.key]
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "orchestrator" {
  name               = "${local.prefix}-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "orchestrator" {
  name = "${local.prefix}-orchestrator-policy"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBScanCompanies"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = aws_dynamodb_table.companies.arn
      },
      {
        Sid      = "SQSSendMessage"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.worker.arn
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.prefix}-worker-policy"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBWriteJobs"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.jobs.arn
      },
      {
        Sid      = "DynamoDBScanCompanies"
        Effect   = "Allow"
        Action   = ["dynamodb:Scan"]
        Resource = aws_dynamodb_table.companies.arn
      },
      {
        Sid    = "SQSReceive"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.worker.arn
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role" "notifier" {
  name               = "${local.prefix}-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy" "notifier" {
  name = "${local.prefix}-notifier-policy"
  role = aws_iam_role.notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBScanJobs"
        Effect = "Allow"
        Action = ["dynamodb:Scan", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.jobs.arn,
          "${aws_dynamodb_table.jobs.arn}/index/*"
        ]
      },
      {
        Sid      = "SESSendEmail"
        Effect   = "Allow"
        Action   = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = "*"
      },
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}


resource "aws_lambda_function" "orchestrator" {
  function_name    = "${local.prefix}-orchestrator"
  role             = aws_iam_role.orchestrator.arn
  handler          = "orchestrator.handler.handler"
  runtime          = "python3.13"
  filename         = "${path.module}/.build/orchestrator.zip"
  source_code_hash = data.external.lambda_build["orchestrator"].result.hash
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      COMPANIES_TABLE  = aws_dynamodb_table.companies.name
      WORKER_QUEUE_URL = aws_sqs_queue.worker.url
    }
  }
}

resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/aws/lambda/${aws_lambda_function.orchestrator.function_name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.prefix}-worker"
  role             = aws_iam_role.worker.arn
  handler          = "worker.handler.handler"
  runtime          = "python3.13"
  filename         = "${path.module}/.build/worker.zip"
  source_code_hash = data.external.lambda_build["worker"].result.hash
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.worker_memory_mb

  environment {
    variables = {
      JOBS_TABLE                 = aws_dynamodb_table.jobs.name
      COMPANIES_TABLE            = aws_dynamodb_table.companies.name
      BUILTIN_LOCATION           = var.builtin_location
      BUILTIN_WORK_TYPE          = var.builtin_work_type
      LOCATION                   = var.location
      WORK_TYPE                  = var.work_type
      TITLE_KEYWORDS             = var.title_keywords
      EXCLUDE_TITLE_KEYWORDS     = var.exclude_title_keywords
      ALLOW_PUBLIC_TRUST         = tostring(var.allow_public_trust)
      ALLOW_SECRET_CLEARANCE     = tostring(var.allow_secret_clearance)
      ALLOW_TOP_SECRET_CLEARANCE = tostring(var.allow_top_secret_clearance)
    }
  }
}

resource "aws_lambda_event_source_mapping" "worker_sqs" {
  event_source_arn = aws_sqs_queue.worker.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 1 # one company per invocation for isolation
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${aws_lambda_function.worker.function_name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "notifier" {
  function_name    = "${local.prefix}-notifier"
  role             = aws_iam_role.notifier.arn
  handler          = "notifier.handler.handler"
  runtime          = "python3.13"
  filename         = "${path.module}/.build/notifier.zip"
  source_code_hash = data.external.lambda_build["notifier"].result.hash
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      JOBS_TABLE       = aws_dynamodb_table.jobs.name
      SES_FROM_ADDRESS = var.ses_from_address
      SES_TO_ADDRESS   = var.ses_to_address
      LOOKBACK_MINUTES = tostring(var.lookback_minutes)
      SES_REGION       = var.aws_region
    }
  }
}

resource "aws_cloudwatch_log_group" "notifier" {
  name              = "/aws/lambda/${aws_lambda_function.notifier.function_name}"
  retention_in_days = 14
}


resource "aws_sqs_queue" "worker_dlq" {
  name                      = "${local.prefix}-worker-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "worker" {
  name                       = "${local.prefix}-worker"
  visibility_timeout_seconds = var.lambda_timeout_seconds + 30
  message_retention_seconds  = 86400 # 1 day
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.worker_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "worker" {
  queue_url = aws_sqs_queue.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowOrchestratorSend"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.orchestrator.arn }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.worker.arn
      }
    ]
  })
}


resource "aws_dynamodb_table" "companies" {
  name                        = "${local.prefix}-companies"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "company_name"
  deletion_protection_enabled = true

  attribute {
    name = "company_name"
    type = "S"
  }

  tags = {
    Name = "${local.prefix}-companies"
  }
}

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.prefix}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  # TODO: add a GSI on discovered_at so the Notifier can do efficient
  # time-range queries instead of a full table scan.
  # attribute {
  #   name = "discovered_at"
  #   type = "S"
  # }
  # global_secondary_index {
  #   name               = "discovered_at-index"
  #   hash_key           = "discovered_at"
  #   projection_type    = "ALL"
  # }

  tags = {
    Name = "${local.prefix}-jobs"
  }
}


data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${local.prefix}-scheduler-policy"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeOrchestratorAndNotifier"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          aws_lambda_function.orchestrator.arn,
          aws_lambda_function.notifier.arn
        ]
      }
    ]
  })
}

# Uses EventBridge Scheduler (not classic EventBridge Rules) so schedule_expression_timezone
# can express these cron times in America/New_York directly — Scheduler handles the DST
# transition automatically, where a plain UTC cron() on a Rule would need manual adjustment
# twice a year.
resource "aws_scheduler_schedule" "orchestrator_weekday" {
  name                         = "${local.prefix}-orchestrator-weekday-schedule"
  schedule_expression          = var.orchestrator_weekday_schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.orchestrator.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "orchestrator_weekend" {
  count = var.orchestrator_weekend_schedule != null ? 1 : 0

  name                         = "${local.prefix}-orchestrator-weekend-schedule"
  schedule_expression          = var.orchestrator_weekend_schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.orchestrator.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "notifier_weekday" {
  name                         = "${local.prefix}-notifier-weekday-schedule"
  schedule_expression          = var.notifier_weekday_schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.notifier.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_scheduler_schedule" "notifier_weekend" {
  count = var.notifier_weekend_schedule != null ? 1 : 0

  name                         = "${local.prefix}-notifier-weekend-schedule"
  schedule_expression          = var.notifier_weekend_schedule
  schedule_expression_timezone = var.schedule_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.notifier.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}


module "cost_widget" {
  count   = var.enable_cost_widget && var.enable_dashboard ? 1 : 0
  source  = "ccliver/cw-cost-widget/aws"
  version = "~> 1.4"

  project_name               = "${local.prefix}-cost-widget"
  cost_allocation_tag_key    = var.cost_allocation_tag_key
  cost_allocation_tag_values = coalesce(var.cost_allocation_tag_values, [var.prefix])
}

resource "aws_lambda_permission" "cost_widget_dashboard" {
  count = var.enable_cost_widget && var.enable_dashboard ? 1 : 0

  statement_id   = "AllowCloudWatchDashboardInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = module.cost_widget[0].lambda_function_name
  principal      = "cloudwatch.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
}

locals {
  # Split out from the dashboard resource below so the cost widget can be appended
  # conditionally via concat() instead of hand-editing this list in place.
  dashboard_widgets = [
    {
      type   = "metric"
      x      = 0
      y      = 0
      width  = 8
      height = 6
      properties = {
        title  = "Orchestrator: Invocations / Errors / Throttles"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Sum"
        period = 300
        metrics = [
          ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.orchestrator.function_name, { label = "Invocations" }],
          ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.orchestrator.function_name, { label = "Errors" }],
          ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.orchestrator.function_name, { label = "Throttles" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 8
      y      = 0
      width  = 8
      height = 6
      properties = {
        title  = "Worker: Invocations / Errors / Throttles"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Sum"
        period = 300
        metrics = [
          ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.worker.function_name, { label = "Invocations" }],
          ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.worker.function_name, { label = "Errors" }],
          ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.worker.function_name, { label = "Throttles" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 16
      y      = 0
      width  = 8
      height = 6
      properties = {
        title  = "Notifier: Invocations / Errors / Throttles"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Sum"
        period = 300
        metrics = [
          ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.notifier.function_name, { label = "Invocations" }],
          ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.notifier.function_name, { label = "Errors" }],
          ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.notifier.function_name, { label = "Throttles" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 6
      width  = 8
      height = 6
      properties = {
        title  = "Lambda: Duration (avg)"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Average"
        period = 300
        metrics = [
          ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.orchestrator.function_name, { label = "Orchestrator" }],
          ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.worker.function_name, { label = "Worker" }],
          ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.notifier.function_name, { label = "Notifier" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 8
      y      = 6
      width  = 8
      height = 6
      properties = {
        title  = "SQS: Queue Depth / Oldest Message Age"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Maximum"
        period = 300
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.worker.name, { label = "Worker Queue Depth" }],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.worker_dlq.name, { label = "DLQ Depth" }],
          ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.worker.name, { label = "Worker Oldest Message Age (s)", yAxis = "right" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 16
      y      = 6
      width  = 8
      height = 6
      properties = {
        title  = "DynamoDB: Consumed Capacity"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Sum"
        period = 300
        metrics = [
          ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", aws_dynamodb_table.jobs.name, { label = "Jobs Read" }],
          ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.jobs.name, { label = "Jobs Write" }],
          ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", aws_dynamodb_table.companies.name, { label = "Companies Read" }],
          ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", aws_dynamodb_table.companies.name, { label = "Companies Write" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 0
      y      = 12
      width  = 12
      height = 6
      properties = {
        title  = "EventBridge Scheduler: Invocation Attempts / Target Errors"
        region = var.aws_region
        view   = "timeSeries"
        stat   = "Sum"
        period = 300
        metrics = [
          # All four schedules run in the "default" schedule group (no group_name set),
          # so this is combined across orchestrator + notifier, weekday + weekend.
          ["AWS/Scheduler", "InvocationAttemptCount", "ScheduleGroup", "default", { label = "Invocation Attempts" }],
          ["AWS/Scheduler", "TargetErrorCount", "ScheduleGroup", "default", { label = "Target Errors" }],
        ]
      }
    },
    {
      type   = "metric"
      x      = 12
      y      = 12
      width  = 12
      height = 6
      properties = {
        title  = "SES: Send / Bounce / Complaint"
        region = var.aws_region
        view   = "timeSeries"
        period = 300
        metrics = [
          ["AWS/SES", "Send", { stat = "Sum", label = "Send" }],
          ["AWS/SES", "Bounce", { stat = "Sum", label = "Bounce" }],
          ["AWS/SES", "Complaint", { stat = "Sum", label = "Complaint" }],
          ["AWS/SES", "Reputation.BounceRate", { stat = "Average", label = "Bounce Rate", yAxis = "right" }],
          ["AWS/SES", "Reputation.ComplaintRate", { stat = "Average", label = "Complaint Rate", yAxis = "right" }],
        ]
      }
    },
    {
      type   = "log"
      x      = 0
      y      = 18
      width  = 24
      height = 6
      properties = {
        title  = "Recent Errors / Warnings (all functions)"
        region = var.aws_region
        view   = "table"
        query  = <<-EOQ
            SOURCE '${aws_cloudwatch_log_group.orchestrator.name}' | SOURCE '${aws_cloudwatch_log_group.worker.name}' | SOURCE '${aws_cloudwatch_log_group.notifier.name}'
            | fields @timestamp, @log, level, message
            | filter level in ["WARNING", "ERROR"]
            | sort @timestamp desc
            | limit 20
          EOQ
      }
    },
    {
      type   = "log"
      x      = 0
      y      = 24
      width  = 12
      height = 6
      properties = {
        title  = "Jobs Written per Day (worker)"
        region = var.aws_region
        view   = "timeSeries"
        query  = <<-EOQ
            SOURCE '${aws_cloudwatch_log_group.worker.name}'
            | filter message = "Worker done"
            | stats sum(jobs_written) as total_jobs_written by bin(1d)
          EOQ
      }
    },
    {
      type   = "log"
      x      = 12
      y      = 24
      width  = 12
      height = 6
      properties = {
        title  = "ATS Fetch / Backend Warnings (worker)"
        region = var.aws_region
        view   = "table"
        query  = <<-EOQ
            SOURCE '${aws_cloudwatch_log_group.worker.name}'
            | filter level = "WARNING"
            | fields @timestamp, message, company, ats, url, error
            | sort @timestamp desc
            | limit 20
          EOQ
      }
    },
  ]

  cost_widget = {
    type   = "custom"
    x      = 0
    y      = 30
    width  = 24
    height = 6
    properties = {
      title    = "Cost (${var.cost_allocation_tag_key} = ${var.prefix})"
      endpoint = var.enable_cost_widget && var.enable_dashboard ? module.cost_widget[0].lambda_function_arn : ""
      params = {
        lookback_days = 30
        granularity   = "MONTHLY"
      }
    }
  }
}

resource "aws_cloudwatch_dashboard" "observability" {
  count = var.enable_dashboard ? 1 : 0

  dashboard_name = "${local.prefix}-observability"

  dashboard_body = jsonencode({
    start = "-P14D"
    widgets = concat(
      local.dashboard_widgets,
      var.enable_cost_widget && var.enable_dashboard ? [local.cost_widget] : []
    )
  })
}
