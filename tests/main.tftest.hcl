# Native `terraform test` suite (Terraform >= 1.7). Runs entirely against a
# mocked AWS provider — no real credentials, no real resources, no cost —
# `data.external.lambda_build` still runs for real (it's a local script, not
# an AWS API call), so these still exercise the actual Lambda-packaging path.
#
# Run with: terraform test

mock_provider "aws" {
  # aws_iam_policy_document is a pure local computation (no real AWS API call
  # even outside tests), but mock_provider mocks it anyway and can't fabricate
  # valid JSON for it — override it explicitly rather than leaving it to the
  # auto-generated (invalid) mock value.
  override_data {
    target = data.aws_iam_policy_document.lambda_assume_role
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_iam_policy_document.scheduler_assume_role
    values = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  ses_from_address = "test@example.com"
  ses_to_address   = "test@example.com"
}

run "plans_successfully_with_defaults" {
  command = plan

  assert {
    condition     = length(aws_lambda_function.orchestrator) >= 0
    error_message = "Module should plan cleanly with only the required variables set"
  }
}

run "dashboard_created_by_default" {
  command = plan

  assert {
    condition     = length(aws_cloudwatch_dashboard.observability) == 1
    error_message = "enable_dashboard defaults to true, so the dashboard should be planned"
  }
}

run "dashboard_omitted_when_disabled" {
  command = plan

  variables {
    enable_dashboard = false
  }

  assert {
    condition     = length(aws_cloudwatch_dashboard.observability) == 0
    error_message = "enable_dashboard = false should result in zero dashboard resources"
  }

  assert {
    condition     = output.dashboard_url == null
    error_message = "dashboard_url output should be null when the dashboard is disabled"
  }
}

run "tracking_api_omitted_by_default" {
  command = plan

  assert {
    condition     = length(aws_apigatewayv2_api.tracker) == 0
    error_message = "enable_tracking_api defaults to false, so no HTTP API should be planned"
  }

  assert {
    condition     = length(aws_lambda_function.tracker) == 0
    error_message = "enable_tracking_api defaults to false, so no tracker Lambda should be planned"
  }

  assert {
    condition     = output.tracking_api_invoke_url == null
    error_message = "tracking_api_invoke_url output should be null when the tracking API is disabled"
  }

  assert {
    condition     = output.tracker_lambda_arn == null
    error_message = "tracker_lambda_arn output should be null when the tracking API is disabled"
  }
}

run "tracking_api_created_when_enabled" {
  command = plan

  variables {
    enable_tracking_api = true
  }

  assert {
    condition     = length(aws_apigatewayv2_api.tracker) == 1
    error_message = "enable_tracking_api = true should plan exactly one HTTP API"
  }

  assert {
    condition     = length(aws_apigatewayv2_route.tracker_list_jobs) == 1
    error_message = "enable_tracking_api = true should plan the GET /jobs route"
  }

  assert {
    condition     = aws_apigatewayv2_route.tracker_list_jobs[0].authorization_type == "AWS_IAM"
    error_message = "Every tracking API route must require AWS_IAM authorization"
  }

  assert {
    condition     = aws_apigatewayv2_route.tracker_get_job[0].authorization_type == "AWS_IAM"
    error_message = "Every tracking API route must require AWS_IAM authorization"
  }

  assert {
    condition     = aws_apigatewayv2_route.tracker_patch_job[0].authorization_type == "AWS_IAM"
    error_message = "Every tracking API route must require AWS_IAM authorization"
  }

  assert {
    condition     = aws_lambda_function.tracker[0].function_name == "req-aggregator-tracker"
    error_message = "Tracker Lambda function name should be derived from the default prefix"
  }
}

run "resources_use_custom_prefix" {
  command = plan

  variables {
    prefix = "custom-prefix"
  }

  assert {
    condition     = aws_lambda_function.worker.function_name == "custom-prefix-worker"
    error_message = "Worker Lambda function name should be derived from the custom prefix"
  }

  assert {
    condition     = aws_dynamodb_table.jobs.name == "custom-prefix-jobs"
    error_message = "Jobs table name should be derived from the custom prefix"
  }
}

run "worker_receives_configured_title_keywords" {
  command = plan

  variables {
    title_keywords         = "registered nurse,rn,np"
    exclude_title_keywords = "cna"
  }

  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["TITLE_KEYWORDS"] == "registered nurse,rn,np"
    error_message = "Worker Lambda should receive the configured TITLE_KEYWORDS env var"
  }

  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["EXCLUDE_TITLE_KEYWORDS"] == "cna"
    error_message = "Worker Lambda should receive the configured EXCLUDE_TITLE_KEYWORDS env var"
  }
}

run "worker_receives_clearance_tier_toggles" {
  command = plan

  variables {
    allow_public_trust         = false
    allow_secret_clearance     = true
    allow_top_secret_clearance = true
  }

  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["ALLOW_PUBLIC_TRUST"] == "false"
    error_message = "Worker Lambda should receive the configured ALLOW_PUBLIC_TRUST env var"
  }

  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["ALLOW_SECRET_CLEARANCE"] == "true"
    error_message = "Worker Lambda should receive the configured ALLOW_SECRET_CLEARANCE env var"
  }

  assert {
    condition     = aws_lambda_function.worker.environment[0].variables["ALLOW_TOP_SECRET_CLEARANCE"] == "true"
    error_message = "Worker Lambda should receive the configured ALLOW_TOP_SECRET_CLEARANCE env var"
  }
}
