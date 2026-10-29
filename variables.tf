variable "name" {
  description = "Unique deployment prefix. Do not reuse the existing Alexa deployment's name."
  type        = string
  default     = "ha-phone-relay"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,39}$", var.name))
    error_message = "Use 1-40 lowercase letters, digits, or hyphens, starting with a letter."
  }
}

variable "aws_account_id" {
  description = "Required expected AWS account ID; prevents using credentials for the wrong account."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Provide the expected 12-digit AWS account ID."
  }
}

variable "aws_region" {
  description = "AWS region supporting API Gateway WebSocket and HTTP APIs."
  type        = string
  default     = "us-east-1"
}

variable "log_retention_days" {
  description = "Lambda diagnostic log retention; request bodies and webhook URLs must not be logged."
  type        = number
  default     = 14
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "Select a supported CloudWatch retention period."
  }
}
