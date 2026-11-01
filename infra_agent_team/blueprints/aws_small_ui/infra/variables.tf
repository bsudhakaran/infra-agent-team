variable "region" {
  type    = string
  default = "us-east-1"
}

variable "app_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.app_name))
    error_message = "app_name must be 2-20 chars of lowercase letters, digits and hyphens."
  }
}

variable "run_id" {
  type = string
}

variable "expires_at" {
  type        = string
  description = "ISO timestamp after which the Cost agent treats this run as a cleanup candidate"
}

variable "owner" {
  type    = string
  default = "infra-agent-team"
}

variable "lambda_memory_mb" {
  type    = number
  default = 128
}

variable "throttle_rate" {
  type    = number
  default = 10
}

variable "throttle_burst" {
  type    = number
  default = 20
}
