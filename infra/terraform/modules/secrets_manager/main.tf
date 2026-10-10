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
    "approval-signing-key", # approval hub ONLY
    "approval-public-key",  # gateway and verifiers
    "anthropic-api-key",
    "youtube-oauth-client",
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

output "platform_secret_ids" {
  value = { for k, s in google_secret_manager_secret.platform : k => s.id }
}
output "channel_secret_ids" {
  value = { for k, s in google_secret_manager_secret.channel_refresh_token : k => s.id }
}
