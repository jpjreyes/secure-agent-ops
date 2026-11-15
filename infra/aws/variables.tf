variable "region" {
  type    = string
  default = "us-west-2"
}

variable "image_tag" {
  type        = string
  description = "image tag in ECR (the git commit)"
}

variable "web_origins" {
  type        = list(string)
  description = "browser origins allowed to call the API (the Vercel site, localhost for development)"
  default     = ["http://localhost:3000"]
}

variable "api_key_param" {
  type    = string
  default = "/secure-agent-ops/ANTHROPIC_API_KEY"
}

variable "runs_per_hour" {
  type    = number
  default = 6
}

variable "runs_per_day" {
  type    = number
  default = 20
}

variable "audit_retention_days" {
  type    = number
  default = 1
}
