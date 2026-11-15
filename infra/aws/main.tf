# Public demo on AWS (DECISIONS D5, D25): one image, two Lambda functions.
#   api    - Function URL, Mangum; creates runs (rate limited), serves run documents, kill switch, audit check
#   worker - invoked asynchronously per run: plants the attack, runs the team through the gateway
# State: DynamoDB (rate limits, per-run kill switches), S3 (run documents), S3 with Object Lock (final audit logs).
# The issuer signs agent tokens with a KMS key (ECC_NIST_P256); the private key never leaves KMS.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {
    bucket       = "sao-tfstate-054896499172"
    key          = "secure-agent-ops/demo.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "secure-agent-ops" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name    = "sao-demo"
  account = data.aws_caller_identity.me.account_id
  image   = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
  # the key itself is never read by Terraform (it would land in the state file); only its ARN is used
  api_key_arn = "arn:aws:ssm:${var.region}:${local.account}:parameter${var.api_key_param}"
}

# ---------- image registry ----------
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep the last 3 images",
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 },
    action       = { type = "expire" }
  }] })
}

# ---------- signing key for the issuer ----------
resource "aws_kms_key" "issuer" {
  description              = "secure-agent-ops demo: agent token issuer (ES256)"
  customer_master_key_spec = "ECC_NIST_P256"
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 7
}

resource "aws_kms_alias" "issuer" {
  name          = "alias/${local.name}-issuer"
  target_key_id = aws_kms_key.issuer.key_id
}

# ---------- state ----------
resource "aws_dynamodb_table" "state" {
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_s3_bucket" "runs" {
  bucket        = "${local.name}-runs-${local.account}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "runs" {
  bucket                  = aws_s3_bucket.runs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "runs" {
  bucket = aws_s3_bucket.runs.id
  rule {
    id     = "expire-runs"
    status = "Enabled"
    filter { prefix = "runs/" }
    expiration { days = 30 }
  }
}

# Final audit logs: Object Lock in compliance mode, so not even the account owner can rewrite or delete a log
# before its retention ends (kept short for a demo).
resource "aws_s3_bucket" "audit" {
  bucket              = "${local.name}-audit-${local.account}"
  object_lock_enabled = true
  force_destroy       = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    id     = "expire-audit"
    status = "Enabled"
    filter { prefix = "audit/" }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}
