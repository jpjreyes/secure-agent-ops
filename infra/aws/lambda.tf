# ---------- roles (least privilege: each function gets only what its code calls) ----------
data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
  statement {
    sid       = "RateLimitsAndKillSwitch"
    actions   = ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    sid       = "RunDocuments"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }
  statement {
    sid       = "MissingRunIs404"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.runs.arn]
  }
  statement {
    sid       = "StartRun"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
  statement {
    sid       = "KillSwitchRead"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.state.arn]
  }
  statement {
    sid       = "RunDocuments"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.runs.arn}/runs/*"]
  }
  statement {
    sid       = "MissingRunIs404"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.runs.arn]
  }
  statement {
    sid       = "LockedAuditLog"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/audit/*"]
  }
  statement {
    sid       = "IssuerSigns"
    actions   = ["kms:Sign", "kms:GetPublicKey"]
    resources = [aws_kms_key.issuer.arn]
  }
  statement {
    sid       = "DemoApiKey"
    actions   = ["ssm:GetParameter"]
    resources = [local.api_key_arn]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = 14
}

locals {
  common_env = {
    SAO_TABLE       = aws_dynamodb_table.state.name
    SAO_RUNS_BUCKET = aws_s3_bucket.runs.bucket
  }
}

# ---------- functions ----------
resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.worker.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["arm64"]
  memory_size   = 3008
  timeout       = 600
  ephemeral_storage { size = 2048 }
  image_config { command = ["sao.demo.worker.handler"] }
  environment {
    variables = merge(local.common_env, {
      SAO_LLM           = "api"
      SAO_API_KEY_PARAM = var.api_key_param
      SAO_KMS_KEY_ID    = aws_kms_key.issuer.arn
      SAO_AUDIT_BUCKET  = aws_s3_bucket.audit.bucket
    })
  }
  depends_on = [aws_cloudwatch_log_group.worker, aws_iam_role_policy.worker]
}

resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name                = aws_lambda_function.worker.function_name
  maximum_retry_attempts       = 0 # a failed run is shown as failed, never re-run (it would spend twice)
  maximum_event_age_in_seconds = 900
}

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.api.arn
  package_type  = "Image"
  image_uri     = local.image
  architectures = ["arm64"]
  memory_size   = 1024
  timeout       = 20
  publish       = true
  snap_start { apply_on = "PublishedVersions" }
  environment {
    variables = merge(local.common_env, {
      SAO_WORKER_FUNCTION = aws_lambda_function.worker.function_name
      SAO_WEB_ORIGINS     = join(",", var.web_origins)
      SAO_RUNS_PER_HOUR   = tostring(var.runs_per_hour)
      SAO_RUNS_PER_DAY    = tostring(var.runs_per_day)
    })
  }
  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.api.function_name
  function_version = aws_lambda_function.api.version
}

resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  qualifier          = aws_lambda_alias.live.name
  authorization_type = "NONE"
  cors {
    allow_origins = var.web_origins
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
    max_age       = 600
  }
}

# Function URLs with auth NONE need both permissions since Oct 2025.
resource "aws_lambda_permission" "url" {
  statement_id           = "PublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  qualifier              = aws_lambda_alias.live.name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "PublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.api.function_name
  qualifier                = aws_lambda_alias.live.name
  principal                = "*"
  invoked_via_function_url = true # only through the URL, not as a plain public invoke
}
