# Complete example

Demonstrates every variable the `terraform-aws-req-aggregator` module accepts, with comments explaining each one. This is documentation you can run, not a ready-to-deploy stack:

- `ses_from_address`/`ses_to_address` are placeholders — replace with addresses verified in SES first.
- There's no backend block, so `terraform apply` here would use local state. A real deployment should have its own root configuration (its own backend, provider, and `terraform.tfvars`) that calls this module the same way `main.tf` does here.

```bash
terraform init
terraform plan
```

`plan` will build the Lambda packages automatically (see `scripts/build-lambda-package.sh` in the module root) and show what a full deployment would create — useful for reviewing the module's behavior without maintaining a second real stack.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

No providers.

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_req_aggregator"></a> [req\_aggregator](#module\_req\_aggregator) | ../.. | n/a |

## Resources

No resources.

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_aws_region"></a> [aws\_region](#input\_aws\_region) | n/a | `string` | `"us-east-1"` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_companies_table_name"></a> [companies\_table\_name](#output\_companies\_table\_name) | n/a |
| <a name="output_dashboard_url"></a> [dashboard\_url](#output\_dashboard\_url) | n/a |
| <a name="output_jobs_table_name"></a> [jobs\_table\_name](#output\_jobs\_table\_name) | n/a |
| <a name="output_tracking_api_invoke_url"></a> [tracking\_api\_invoke\_url](#output\_tracking\_api\_invoke\_url) | n/a |
<!-- END_TF_DOCS -->
