terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "region" { type = string }
variable "env" { type = string }
variable "subnet_id" { type = string }
variable "images" {
  type        = map(string)
  description = "Container image per service: approval_hub, gateway, worker."
}
variable "platform_secret_ids" { type = map(string) }
variable "channel_secret_ids" { type = map(string) }

# One service account per component: least privilege and separate trust boundaries.
resource "google_service_account" "svc" {
  for_each     = toset(["approval-hub", "gateway", "worker"])
  project      = var.project_id
  account_id   = "${each.value}-${var.env}"
  display_name = "${each.value} (${var.env})"
}

# The approval hub alone can read the private signing key.
resource "google_secret_manager_secret_iam_member" "hub_signing_key" {
  secret_id = var.platform_secret_ids["approval-signing-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["approval-hub"].email}"
}

# The gateway reads only the PUBLIC verification key and YouTube OAuth material.
resource "google_secret_manager_secret_iam_member" "gateway_public_key" {
  secret_id = var.platform_secret_ids["approval-public-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_oauth_client" {
  secret_id = var.platform_secret_ids["youtube-oauth-client"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "gateway_channel_tokens" {
  for_each  = var.channel_secret_ids
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["gateway"].email}"
}

resource "google_secret_manager_secret_iam_member" "worker_llm_key" {
  secret_id = var.platform_secret_ids["anthropic-api-key"]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["worker"].email}"
}

resource "google_cloud_run_v2_service" "svc" {
  for_each = {
    "approval-hub" = var.images["approval_hub"]
    "gateway"      = var.images["gateway"]
    "worker"       = var.images["worker"]
  }
  project  = var.project_id
  name     = "${each.key}-${var.env}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.svc[each.key].email
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        subnetwork = var.subnet_id
      }
    }
    containers {
      image = each.value
      env {
        name  = "ENV"
        value = var.env
      }
    }
  }
}

output "service_accounts" {
  value = { for k, sa in google_service_account.svc : k => sa.email }
}
