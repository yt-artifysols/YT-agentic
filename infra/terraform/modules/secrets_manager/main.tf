terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "channel_ids" {
  type        = list(string)
  description = "Channels that get their own OAuth refresh-token secret."
}

# Secret CONTAINERS only. Values are added out-of-band by an operator, never through Terraform,
# so no secret material ever lands in state or in the repo.
locals {
  platform_secrets = [
    # The approval signing key is NOT a secret here: it lives in Cloud KMS and never leaves it.
    "approval-public-key", # gateway and verifiers
    "anthropic-api-key",
    "youtube-oauth-client",
    # One database login per service, each mapped to exactly one database role (yt_hub, yt_worker,
    # yt_gateway, yt_ops; migration 014): a compromised worker cannot approve or publish.
    "database-url-hub",
    "database-url-worker",
    "database-url-gateway",
    "database-url-ops",
    "slack-webhook", # approval hub notifications
  ]
}

resource "google_secret_manager_secret" "platform" {
  for_each  = toset(local.platform_secrets)
  project   = var.project_id
  secret_id = each.value
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "channel_refresh_token" {
  for_each  = toset(var.channel_ids)
  project   = var.project_id
  secret_id = "youtube-refresh-token-${each.value}"
  labels    = { channel = each.value }
  replication {
    auto {}
  }
}

# Read-only analytics token per channel: SEPARATE from the upload token only the gateway reads.
resource "google_secret_manager_secret" "channel_analytics_token" {
  for_each  = toset(var.channel_ids)
  project   = var.project_id
  secret_id = "youtube-analytics-token-${each.value}"
  labels    = { channel = each.value }
  replication {
    auto {}
  }
}

output "analytics_secret_ids" {
  value = { for k, s in google_secret_manager_secret.channel_analytics_token : k => s.id }
}
output "platform_secret_ids" {
  value = { for k, s in google_secret_manager_secret.platform : k => s.id }
}
output "channel_secret_ids" {
  value = { for k, s in google_secret_manager_secret.channel_refresh_token : k => s.id }
}
