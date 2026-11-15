output "api_url" { value = aws_lambda_function_url.api.function_url }
output "ecr_repo" { value = aws_ecr_repository.app.repository_url }
output "audit_bucket" { value = aws_s3_bucket.audit.bucket }
output "issuer_key" { value = aws_kms_alias.issuer.name }
