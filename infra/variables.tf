variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into."
}

variable "location" {
  type        = string
  description = "Azure region for all resources."
  default     = "eastus2"
}

variable "resource_group_name" {
  type        = string
  description = "Name of the new resource group to create."
  default     = "rg-hivtools-mcp-prod"
}

variable "name_prefix" {
  type        = string
  description = "Prefix for resource names."
  default     = "hivtools-mcp"
}

variable "github_repository" {
  type        = string
  description = "owner/repo the GitHub Actions OIDC federation trusts."
  default     = "hivtools/hivtools-mcp"
}

# GitHub's OIDC subject claim for this repo is immutable (opted in, or created
# after the July 2026 default change): it embeds these numeric IDs rather than
# just the owner/repo names, so a renamed or deleted-and-recreated repo can't
# inherit this trust. Read the exact format GitHub sends with:
#   gh api repos/<owner>/<repo>/actions/oidc/customization/sub
variable "github_owner_id" {
  type        = string
  description = "Numeric GitHub owner (org/user) ID, for the immutable OIDC subject claim."
  default     = "172818267"
}

variable "github_repo_id" {
  type        = string
  description = "Numeric GitHub repository ID, for the immutable OIDC subject claim."
  default     = "1363874738"
}

variable "github_environment" {
  type        = string
  description = "GitHub deployment environment the release workflow runs in (federation subject)."
  default     = "production"
}

variable "container_image" {
  type        = string
  description = "Image the Container App runs. A placeholder at first apply; the release workflow replaces it (Terraform ignores changes to it thereafter)."
  default     = "mcr.microsoft.com/k8se/quickstart-full:latest"
}

variable "container_registry_name" {
  type        = string
  description = "Azure Container Registry name (globally unique, alphanumeric only). Leave null to generate one."
  default     = null
}

variable "allowed_ingress_cidrs" {
  type        = list(string)
  description = "If non-empty, only these CIDRs may reach the app (everything else is denied). Empty = open. Set to Cloudflare's ranges if you put Cloudflare in front."
  default     = []
}

variable "min_replicas" {
  type        = number
  description = "0 = scale to zero when idle (no compute cost)."
  default     = 0
}

variable "max_replicas" {
  type        = number
  description = "Hard ceiling on replicas - the main guard on runaway compute/egress cost."
  default     = 2
}

variable "http_concurrency" {
  type        = number
  description = "Concurrent requests per replica before scaling out."
  default     = 20
}

variable "cpu" {
  type        = number
  description = "vCPU per replica."
  default     = 0.5
}

variable "memory" {
  type        = string
  description = "Memory per replica (must pair with cpu per the ACA allowed combinations)."
  default     = "1Gi"
}

variable "log_retention_days" {
  type        = number
  description = "Log Analytics retention."
  default     = 30
}

variable "log_daily_quota_gb" {
  type        = number
  description = "Log Analytics daily ingestion cap (caps the one Azure cost that scales with a request flood). -1 for unlimited."
  default     = 1
}

variable "budget_amount" {
  type        = number
  description = "Monthly budget for the resource group, in the subscription's billing currency. Alert only - Azure budgets never stop spend."
  default     = 20
}

variable "budget_start_date" {
  type        = string
  description = <<-EOT
    Anchor for the monthly budget period - must be the 1st of a month. Azure
    keeps generating subsequent months on its own, so once the budget exists
    this is never touched again (see the ignore_changes in budget.tf) and
    doesn't need updating. It only matters again if the budget resource is
    created fresh - Azure requires a *new* budget's start_date to be within 12
    months in the past / 3 months in the future of the apply, so bump this to
    the current month if a from-scratch apply ever rejects it.
  EOT
  default     = "2026-09-01T00:00:00Z"
}

variable "budget_alert_email" {
  type        = string
  description = "Where budget threshold alerts go."
  default     = "rashton@avenirhealth.org"
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to every resource."
  default = {
    project    = "hivtools-mcp"
    managed_by = "terraform"
  }
}
