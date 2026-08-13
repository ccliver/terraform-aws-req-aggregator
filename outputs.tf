output "companies_table_name" {
  description = "DynamoDB companies table name"
  value       = aws_dynamodb_table.companies.name
}

output "jobs_table_name" {
  description = "DynamoDB jobs table name"
  value       = aws_dynamodb_table.jobs.name
}

output "worker_queue_url" {
  description = "SQS queue URL for the Worker Lambda"
  value       = aws_sqs_queue.worker.url
}

output "worker_dlq_url" {
  description = "SQS dead-letter queue URL for failed Worker messages"
  value       = aws_sqs_queue.worker_dlq.url
}

output "orchestrator_lambda_arn" {
  description = "ARN of the Orchestrator Lambda"
  value       = aws_lambda_function.orchestrator.arn
}

output "worker_lambda_arn" {
  description = "ARN of the Worker Lambda"
  value       = aws_lambda_function.worker.arn
}

output "worker_function_name" {
  description = "Function name of the Worker Lambda"
  value       = aws_lambda_function.worker.function_name
}

output "notifier_lambda_arn" {
  description = "ARN of the Notifier Lambda"
  value       = aws_lambda_function.notifier.arn
}

output "dashboard_url" {
  description = "Console URL for the CloudWatch observability dashboard, or null if enable_dashboard is false"
  value       = var.enable_dashboard ? "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.observability[0].dashboard_name}" : null
}

output "cost_widget_lambda_arn" {
  description = "ARN of the cost widget Lambda, or null if enable_cost_widget/enable_dashboard is false"
  value       = var.enable_cost_widget && var.enable_dashboard ? module.cost_widget[0].lambda_function_arn : null
}
